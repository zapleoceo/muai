"""Серверный CLI хаба (внутри контейнера vera3-mcp-hub). Секрет читается из stdin,
не из аргументов: argv видно в `ps` и в истории shell.

    printf %s "$TOKEN" | docker exec -i vera3-mcp-hub python -m vera_hub.cli secret vera/room
    docker exec vera3-mcp-hub python -m vera_hub.cli agent-create probe sniffer/catalog
    docker exec vera3-mcp-hub python -m vera_hub.cli agent-set probe --disable
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from vera_shared.db.engine import close_engine, init_engine
from vera_shared.hub.repo_agents import create_agent, list_agents, update_agent
from vera_shared.hub.repo_upstreams import list_upstreams, save_upstream
from vera_shared.hub.spec import UpstreamSpec

_SPEC_FIELDS = ("project", "slug", "title", "transport", "url", "auth_kind", "auth_header",
                "enabled", "hint", "timeout_s", "max_concurrency")


async def set_secret(target: str, secret: str) -> str:
    project, _, slug = target.partition("/")
    row = next((u for u in await list_upstreams() if (u.project, u.slug) == (project, slug)), None)
    if row is None:
        raise SystemExit(f"no upstream {target}")
    if not secret:
        raise SystemExit("empty secret on stdin")
    spec = UpstreamSpec(**{k: getattr(row, k) for k in _SPEC_FIELDS}, command=list(row.command))
    await save_upstream(spec, row.id, secret=secret)
    return f"secret stored for {target} (encrypted)"


async def set_agent(name: str, allow: list[str] | None, enabled: bool | None) -> str:
    row = next((a for a in await list_agents() if a.name == name), None)
    if row is None:
        raise SystemExit(f"no agent {name}")
    await update_agent(row.id, allow if allow is not None else list(row.allow),
                       row.enabled if enabled is None else enabled, row.note)
    return f"agent {name} updated"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vera_hub.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sec = sub.add_parser("secret", help="store an upstream secret read from stdin")
    sec.add_argument("target", help="project/slug")
    new = sub.add_parser("agent-create", help="create an agent; prints its token once")
    new.add_argument("name")
    new.add_argument("allow", nargs="*", help="project or project/upstream")
    upd = sub.add_parser("agent-set", help="change grants or switch an agent off/on")
    upd.add_argument("name")
    upd.add_argument("--allow", nargs="*")
    upd.add_argument("--disable", action="store_true")
    upd.add_argument("--enable", action="store_true")
    return parser


async def _run(args: argparse.Namespace) -> str:
    await init_engine()
    try:
        if args.cmd == "secret":
            return await set_secret(args.target, sys.stdin.read().strip())
        if args.cmd == "agent-create":
            return await create_agent(args.name, args.allow, note="created via cli")
        enabled = False if args.disable else (True if args.enable else None)
        return await set_agent(args.name, args.allow, enabled)
    finally:
        await close_engine()


def main(argv: list[str] | None = None) -> None:
    print(asyncio.run(_run(_parser().parse_args(argv))))


if __name__ == "__main__":
    main()
