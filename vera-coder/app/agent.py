"""Claude-driven code agent.

Loop:
  1. Spin up a clean worktree from a git mirror at <work_dir>.
  2. Create branch auto/<slug>.
  3. Run anthropic tool-use loop with FS+bash+git tools.
  4. On finish: pytest. If green → commit + push branch + open PR via gh.
  5. DM Dima a summary via vera-core internal endpoint.
"""
import asyncio
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx
from anthropic import AsyncAnthropic

from app.config import get_settings
from app.tools import (edit_file, git_diff, list_dir, pytest_run, read_file,
                        run_bash, write_file)

log = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-5-20250929"

_TOOL_SCHEMAS = [
    {"name": "read_file",
     "description": "Read a file in the worktree.",
     "input_schema": {"type": "object", "properties": {
         "path": {"type": "string"}}, "required": ["path"]}},
    {"name": "write_file",
     "description": "Create or overwrite a file in the worktree.",
     "input_schema": {"type": "object", "properties": {
         "path": {"type": "string"}, "content": {"type": "string"}},
         "required": ["path", "content"]}},
    {"name": "edit_file",
     "description": "Replace one occurrence of `old` with `new` in file.",
     "input_schema": {"type": "object", "properties": {
         "path": {"type": "string"}, "old": {"type": "string"},
         "new": {"type": "string"}},
         "required": ["path", "old", "new"]}},
    {"name": "list_dir",
     "description": "List files in a directory.",
     "input_schema": {"type": "object", "properties": {
         "path": {"type": "string"}}, "required": []}},
    {"name": "bash",
     "description": "Run a bash command (60s timeout, denylisted commands).",
     "input_schema": {"type": "object", "properties": {
         "command": {"type": "string"}}, "required": ["command"]}},
    {"name": "pytest",
     "description": "Run vera-core pytest. Returns exit/stdout/stderr.",
     "input_schema": {"type": "object", "properties": {
         "args": {"type": "string", "default": "tests -q --tb=short"}}}},
    {"name": "git_diff",
     "description": "Show current diff vs HEAD.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "finish",
     "description": "Done. Provide a one-line PR title + a markdown body.",
     "input_schema": {"type": "object", "properties": {
         "pr_title": {"type": "string"},
         "pr_body": {"type": "string"},
         "summary": {"type": "string"}},
         "required": ["pr_title", "pr_body", "summary"]}},
]

_SYSTEM = """Ты — vera-coder. Тебе дают задание изменить код проекта Vera.

ВАЖНО:
- Все правки делаешь в worktree, не выходи за его пределы.
- Запрещено трогать: .env, .github/workflows/, scripts/deploy.sh, secrets/.
- После всех правок ОБЯЗАТЕЛЬНО запусти pytest. Если красный — пофикси.
- Когда тесты зелёные и изменения готовы — вызови finish с pr_title + body.
- Не торопись: сначала прочитай нужные файлы, затем редактируй.
- Не редактируй больше {max_changes} файлов на одну задачу.
- Если не понимаешь как сделать — finish с summary "не справился, нужен Дима".
- Стиль кода: см. /work/CLAUDE.md и /work/.claude/rules/python.md.
"""


async def _gh_token() -> str | None:
    """Look up GitHub PAT from vera-core (same source as system_deploy)."""
    cfg = get_settings()
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(
                f"{cfg.vera_core_url}/internal/coder/github-token",
                headers={"X-Internal-Secret": cfg.internal_secret},
            )
        if r.status_code == 200:
            return r.json().get("token")
    except Exception as exc:
        log.warning("gh token lookup failed: %s", exc)
    return os.environ.get("GITHUB_PERSONAL_ACCESS_TOKEN") or \
           os.environ.get("GITHUB_TOKEN")


async def _anthropic_key() -> str | None:
    """Same anthropic key Vera uses for chat."""
    cfg = get_settings()
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(
                f"{cfg.vera_core_url}/internal/coder/anthropic-key",
                headers={"X-Internal-Secret": cfg.internal_secret},
            )
        if r.status_code == 200:
            return r.json().get("key")
    except Exception as exc:
        log.warning("anthropic key lookup failed: %s", exc)
    return os.environ.get("ANTHROPIC_API_KEY")


async def _dispatch_tool(name: str, inp: dict, work_dir: str) -> Any:
    if name == "read_file":
        return read_file(work_dir, inp["path"])
    if name == "write_file":
        return write_file(work_dir, inp["path"], inp["content"])
    if name == "edit_file":
        return edit_file(work_dir, inp["path"], inp["old"], inp["new"])
    if name == "list_dir":
        return list_dir(work_dir, inp.get("path", "."))
    if name == "bash":
        return run_bash(work_dir, inp["command"])
    if name == "pytest":
        return pytest_run(work_dir, inp.get("args", "tests -q --tb=short"))
    if name == "git_diff":
        return git_diff(work_dir)
    return {"error": f"unknown tool {name}"}


def _slug(s: str) -> str:
    s = re.sub(r"[^a-zA-Zа-яА-Я0-9 -]", "", s.lower())
    s = re.sub(r"\s+", "-", s).strip("-")
    return s[:40] or f"auto-{int(time.time())}"


def _setup_worktree(work_dir: str, gh_token: str, branch: str) -> None:
    cfg = get_settings()
    if Path(work_dir).exists():
        shutil.rmtree(work_dir)
    auth_url = cfg.repo_url.replace(
        "https://github.com/", f"https://x-access-token:{gh_token}@github.com/",
    )
    subprocess.run(["git", "clone", "--depth", "50", "-b", cfg.repo_branch_base,
                     auth_url, work_dir],
                    check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", work_dir, "checkout", "-b", branch],
                    check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", work_dir, "config", "user.email",
                     "vera-coder@dima.veranda.my"], check=True)
    subprocess.run(["git", "-C", work_dir, "config", "user.name",
                     "vera-coder"], check=True)


async def run_task(task: str, requested_by: str = "dima") -> dict:
    cfg = get_settings()
    gh_token = await _gh_token()
    anthropic_key = await _anthropic_key()
    if not gh_token or not anthropic_key:
        return {"ok": False, "error": "missing GH or Anthropic credentials"}

    slug = _slug(task)
    branch = f"auto/{slug}-{int(time.time()) % 100000}"
    work_dir = os.path.join(cfg.work_dir, slug + f"-{int(time.time())}")
    try:
        _setup_worktree(work_dir, gh_token, branch)
    except subprocess.CalledProcessError as exc:
        return {"ok": False,
                "error": f"git setup failed: {exc.stderr or exc}"}

    client = AsyncAnthropic(api_key=anthropic_key)
    messages: list[dict] = [{
        "role": "user",
        "content": (f"ЗАДАНИЕ от {requested_by}:\n\n{task}\n\n"
                     f"Ветка: {branch}. Worktree: {work_dir}. Поехали."),
    }]
    system = _SYSTEM.format(max_changes=cfg.max_changes_per_task)

    final_summary = None
    pr_title = None
    pr_body = None

    for step in range(cfg.max_iterations):
        try:
            resp = await client.messages.create(
                model=MODEL, max_tokens=4096, system=system,
                tools=_TOOL_SCHEMAS, messages=messages,
            )
        except Exception as exc:
            return {"ok": False, "error": f"claude call failed: {exc}",
                    "branch": branch}

        # Build assistant message
        messages.append({"role": "assistant", "content": resp.content})
        tool_uses = [b for b in resp.content if b.type == "tool_use"]
        if not tool_uses:
            break  # plain text response, agent decided to stop

        tool_results = []
        for tu in tool_uses:
            if tu.name == "finish":
                final_summary = tu.input.get("summary")
                pr_title = tu.input.get("pr_title")
                pr_body = tu.input.get("pr_body")
                tool_results.append({"type": "tool_result", "tool_use_id": tu.id,
                                      "content": "ok, exiting"})
                break
            try:
                out = await _dispatch_tool(tu.name, tu.input, work_dir)
            except Exception as exc:
                out = {"error": f"{type(exc).__name__}: {exc}"}
            tool_results.append({"type": "tool_result", "tool_use_id": tu.id,
                                  "content": json.dumps(out, ensure_ascii=False,
                                                          default=str)[:6000]})
        messages.append({"role": "user", "content": tool_results})
        if final_summary is not None:
            break

    if final_summary is None:
        return {"ok": False, "branch": branch,
                "error": f"hit max_iterations ({cfg.max_iterations}) without finish"}

    # Commit + push + PR
    pytest_check = pytest_run(work_dir)
    if not pytest_check.get("ok"):
        return {"ok": False, "branch": branch, "summary": final_summary,
                "error": "pytest red on final check",
                "pytest_tail": pytest_check.get("stdout", "")[-1500:]}

    commit_msg = f"{pr_title}\n\nAuto-generated by vera-coder for: {task}\n"
    subprocess.run(["git", "-C", work_dir, "add", "-A"], check=True)
    commit = subprocess.run(["git", "-C", work_dir, "commit", "-m", commit_msg],
                             capture_output=True, text=True)
    if commit.returncode != 0:
        return {"ok": False, "branch": branch, "summary": final_summary,
                "error": f"commit failed: {commit.stderr}"}
    push = subprocess.run(["git", "-C", work_dir, "push", "-u", "origin", branch],
                           capture_output=True, text=True)
    if push.returncode != 0:
        return {"ok": False, "branch": branch, "summary": final_summary,
                "error": f"push failed: {push.stderr}"}

    env = {**os.environ, "GH_TOKEN": gh_token}
    pr = subprocess.run(
        ["gh", "pr", "create", "-B", cfg.repo_branch_base, "-H", branch,
         "-t", pr_title or f"vera-coder: {slug}",
         "-b", pr_body or final_summary or "Auto-generated PR"],
        cwd=work_dir, env=env, capture_output=True, text=True,
    )
    pr_url = pr.stdout.strip() if pr.returncode == 0 else None

    return {"ok": True, "branch": branch, "summary": final_summary,
            "pr_url": pr_url, "pr_stderr": pr.stderr if pr.returncode else None}
