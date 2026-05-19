import asyncio
import logging

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)


async def _git(*args: str) -> tuple[int, str, str]:
    cfg = get_settings()
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", cfg.repo_path, *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


async def git_status() -> dict:
    code, out, err = await _git("status", "--porcelain")
    if code != 0:
        return {"error": err.strip(), "files": []}
    files = []
    for line in out.splitlines():
        if not line.strip():
            continue
        state = line[:2].strip()
        path = line[3:]
        files.append({"state": state, "path": path})
    return {"clean": not files, "files": files}


async def git_log(limit: int = 10) -> dict:
    code, out, err = await _git(
        "log", f"-n{int(limit)}", "--pretty=format:%h%x09%an%x09%ai%x09%s"
    )
    if code != 0:
        return {"error": err.strip(), "commits": []}
    commits = []
    for line in out.splitlines():
        parts = line.split("\t", 3)
        if len(parts) == 4:
            commits.append({
                "hash": parts[0], "author": parts[1],
                "date": parts[2], "message": parts[3],
            })
    return {"commits": commits}


async def git_diff(path: str = "") -> dict:
    args = ["diff", "--stat"]
    if path:
        args += ["--", path]
    code, out, err = await _git(*args)
    if code != 0:
        return {"error": err.strip(), "diff": ""}
    return {"diff": out.strip() or "(no changes)"}


async def deploy_status() -> dict:
    info: dict = {}
    code, out, _ = await _git("rev-parse", "HEAD")
    info["head"] = out.strip() if code == 0 else "?"
    code, out, _ = await _git("log", "-1", "--pretty=format:%s")
    info["head_subject"] = out.strip() if code == 0 else "?"
    return info


async def deploy_trigger() -> dict:
    cfg = get_settings()
    headers = {"Authorization": f"Bearer {cfg.deploy_secret}"}
    try:
        async with httpx.AsyncClient(timeout=180) as client:
            r = await client.post(cfg.deploy_url, json={}, headers=headers)
        return {"status_code": r.status_code, "body": r.text[:1000]}
    except Exception as exc:
        logger.exception("deploy trigger failed")
        return {"error": str(exc)}
