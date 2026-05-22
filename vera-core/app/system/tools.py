"""Self-tools exposed by vera-core through the agent registry.
Lets Vera deploy herself, check her own state, refresh her caches."""
import asyncio
import json
import logging
import os
from datetime import datetime

import httpx

log = logging.getLogger(__name__)

_GH_REPO = "zapleoceo/muai"
_DEFAULT_WORKFLOW = "deploy.yml"


async def _gh_token() -> str | None:
    """GitHub PAT lookup order:
      1. mcp_servers row named 'github' (where the user already entered it)
      2. GITHUB_PERSONAL_ACCESS_TOKEN env
      3. GITHUB_TOKEN env
    Means Dima doesn't have to enter the token twice."""
    try:
        from sqlalchemy import select
        from vera_shared.db.engine import get_session
        from vera_shared.db.models import MCPServer
        async with get_session() as s:
            r = await s.execute(select(MCPServer).where(MCPServer.name == "github"))
            row = r.scalar_one_or_none()
            if row and row.env:
                tok = row.env.get("GITHUB_PERSONAL_ACCESS_TOKEN")
                if tok:
                    return tok
    except Exception as exc:
        log.debug("github MCP token lookup failed: %s", exc)
    return os.environ.get("GITHUB_PERSONAL_ACCESS_TOKEN") or \
           os.environ.get("GITHUB_TOKEN") or None


async def system_deploy(ref: str = "master",
                          message: str | None = None) -> dict:
    """Trigger the Deploy workflow on GitHub. Re-uses the same PAT that
    powers the github MCP. Does NOT touch the repo working tree —
    deploy.sh on the server takes care of pull/build/test/rollback."""
    token = await _gh_token()
    if not token:
        return {"ok": False, "error": "no GITHUB_PERSONAL_ACCESS_TOKEN in env"}
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    url = (f"https://api.github.com/repos/{_GH_REPO}/actions/workflows/"
           f"{_DEFAULT_WORKFLOW}/dispatches")
    payload = {"ref": ref}
    if message:
        payload["inputs"] = {"message": message[:200]}
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(url, headers=headers, json=payload)
    if r.status_code in (200, 201, 202, 204):
        return {"ok": True, "triggered_at": datetime.utcnow().isoformat(),
                "ref": ref, "message": message,
                "actions_url": f"https://github.com/{_GH_REPO}/actions"}
    return {"ok": False,
            "error": f"GitHub API {r.status_code}: {r.text[:200]}"}


async def system_status() -> dict:
    """Return git HEAD + last 5 deploy runs + container statuses."""
    token = await _gh_token()
    out: dict = {"now": datetime.utcnow().isoformat()}
    # Local git HEAD (from mounted /var/www/vera)
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", "/var/www/vera", "log", "-1", "--oneline",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        sout, _ = await proc.communicate()
        out["server_head"] = sout.decode().strip()
    except Exception as exc:
        out["server_head"] = f"err: {exc}"
    if token:
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(
                    f"https://api.github.com/repos/{_GH_REPO}/actions/"
                    f"workflows/{_DEFAULT_WORKFLOW}/runs?per_page=5",
                    headers={"Authorization": f"Bearer {token}",
                             "Accept": "application/vnd.github+json"},
                )
            data = r.json() if r.status_code == 200 else {}
            out["recent_runs"] = [
                {"id": w["id"], "status": w["status"],
                 "conclusion": w.get("conclusion"),
                 "head_branch": w["head_branch"],
                 "head_sha": w["head_sha"][:7],
                 "created_at": w["created_at"]}
                for w in (data.get("workflow_runs") or [])[:5]
            ]
        except Exception as exc:
            out["recent_runs"] = f"err: {exc}"
    return {"ok": True, "result": out}


async def vera_set_pref(key: str, value) -> dict:
    """Toggle a user preference. Lets Vera flip her own behaviour when
    Dima asks her in plain text (e.g. «удаляй карточку после реакции»)."""
    from app.bot import preferences
    if key not in preferences.known_keys():
        return {"ok": False,
                "error": f"unknown pref {key!r}, allowed: {preferences.known_keys()}"}
    # Coerce truthy strings into bool for boolean prefs
    if isinstance(value, str) and value.lower() in ("true", "false", "1", "0", "yes", "no", "да", "нет", "вкл", "выкл"):
        value = value.lower() in ("true", "1", "yes", "да", "вкл")
    await preferences.set(key, value)
    return {"ok": True, "key": key, "value": value, "all": await preferences.get_all()}


async def vera_get_prefs() -> dict:
    from app.bot import preferences
    return {"ok": True, "result": await preferences.get_all()}


HANDLERS = {
    "system_deploy": system_deploy,
    "system_status": system_status,
    "vera_set_pref": vera_set_pref,
    "vera_get_prefs": vera_get_prefs,
}
