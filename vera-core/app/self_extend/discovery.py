"""Discovery: take a free-text capability description, find candidate MCP
packages via npm search + a small curated allowlist for Python (uvx) tools.
Returns ranked candidates."""
import asyncio
import json
import logging
from typing import Any

log = logging.getLogger(__name__)

# Curated allowlist for uvx (Python) MCP servers — npm search won't find these.
_UVX_CATALOG: list[dict] = [
    {"name": "mcp-server-fetch", "command": ["uvx", "mcp-server-fetch"],
     "description": "Read web pages as Markdown.", "env_required": []},
    {"name": "mcp-server-git",
     "command": ["uvx", "mcp-server-git", "--repository", "/var/www/vera"],
     "description": "Git operations.", "env_required": []},
    {"name": "mcp-server-time", "command": ["uvx", "mcp-server-time"],
     "description": "Timezone-aware time and date.", "env_required": []},
    {"name": "mcp-server-sqlite",
     "command": ["uvx", "mcp-server-sqlite", "--db-path", "/data/vera.db"],
     "description": "Query a sqlite database (read-only recommended).",
     "env_required": []},
]


def _looks_like_mcp(pkg: dict) -> bool:
    name = (pkg.get("name") or "").lower()
    desc = (pkg.get("description") or "").lower()
    if "mcp" not in name and "mcp" not in desc:
        return False
    # Must be a server / tool / provider, not a library wrapper
    bad = ("client", "sdk-only", "schema", "types")
    return not any(b in name for b in bad)


def _score(pkg: dict, query_tokens: set[str]) -> float:
    name = (pkg.get("name") or "").lower()
    desc = (pkg.get("description") or "").lower()
    text = name + " " + desc
    overlap = sum(1 for t in query_tokens if t in text)
    # npm provides a 'searchScore' or 'score' field; use it as bonus
    npm_score = pkg.get("score") or 0
    if isinstance(npm_score, dict):
        npm_score = npm_score.get("final", 0)
    try:
        npm_score = float(npm_score)
    except (TypeError, ValueError):
        npm_score = 0.0
    return overlap * 10 + npm_score * 5


async def _npm_search(query: str, limit: int = 25) -> list[dict]:
    proc = await asyncio.create_subprocess_exec(
        "npm", "search", query, "--json", "--searchlimit", str(limit),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
    except asyncio.TimeoutError:
        proc.kill()
        return []
    try:
        data = json.loads(stdout.decode() or "[]")
        return data if isinstance(data, list) else []
    except Exception as exc:
        log.warning("npm search parse failed: %s", exc)
        return []


def _match_uvx(query: str, query_tokens: set[str]) -> list[dict]:
    out = []
    for entry in _UVX_CATALOG:
        text = (entry["name"] + " " + entry["description"]).lower()
        if any(t in text for t in query_tokens):
            out.append({
                **entry,
                "source": "uvx-catalog",
                "score": 50.0,  # high baseline: curated
            })
    return out


async def discover(capability: str, top_n: int = 3) -> list[dict]:
    """Return ranked candidates for the given free-text capability.
    Each candidate: {name, command, env_required, description, source, score}."""
    query = capability.strip()
    if not query:
        return []
    tokens = {t.lower() for t in query.split() if len(t) >= 3}

    uvx = _match_uvx(query, tokens)

    raw = await _npm_search(query + " mcp")
    candidates: list[dict] = []
    for pkg in raw:
        if not _looks_like_mcp(pkg):
            continue
        candidates.append({
            "name": pkg["name"],
            "command": ["npx", "-y", pkg["name"]],
            "env_required": [],  # unknown; user fills if needed at install time
            "description": (pkg.get("description") or "")[:300],
            "source": "npm",
            "score": _score(pkg, tokens),
            "version": pkg.get("version"),
            "publisher": (pkg.get("publisher") or {}).get("username"),
            "date": pkg.get("date"),
        })

    merged = uvx + candidates
    merged.sort(key=lambda x: x["score"], reverse=True)
    return merged[:top_n]
