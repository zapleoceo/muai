"""Safe filesystem + bash tools exposed to the Claude agent.

Every write/edit is scoped to the active worktree; the agent cannot
escape it. Bash runs with a 60s timeout and a denylist of dangerous
commands. Git operations are stateless wrappers — branch is set up
by agent.run_task() before the loop starts.
"""
import fnmatch
import logging
import os
import shlex
import subprocess
from pathlib import Path

from app.config import get_settings

log = logging.getLogger(__name__)

_BASH_DENY = ("rm -rf /", ":(){:", "mkfs", "dd if=", "shutdown", "reboot",
              "curl ", "wget ", "/dev/sd")
_FORBIDDEN_BASH_FLAGS = ("--force", "--no-verify")


def _safe_path(work_dir: str, rel: str) -> Path:
    cfg = get_settings()
    root = Path(work_dir).resolve()
    p = (root / rel).resolve()
    # Must stay inside worktree
    if not str(p).startswith(str(root)):
        raise ValueError(f"path {rel} escapes worktree")
    # Must not match forbidden patterns
    rel_inside = str(p.relative_to(root))
    for pat in cfg.forbidden_paths:
        if fnmatch.fnmatch(rel_inside, pat) or rel_inside.startswith(pat):
            raise ValueError(f"path {rel} is in forbidden scope")
    return p


def read_file(work_dir: str, path: str) -> str:
    p = _safe_path(work_dir, path)
    if not p.exists():
        return f"<file not found: {path}>"
    return p.read_text(encoding="utf-8", errors="replace")


def write_file(work_dir: str, path: str, content: str) -> dict:
    p = _safe_path(work_dir, path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return {"ok": True, "path": str(p.relative_to(Path(work_dir).resolve())),
            "bytes": len(content)}


def edit_file(work_dir: str, path: str, old: str, new: str) -> dict:
    p = _safe_path(work_dir, path)
    text = p.read_text(encoding="utf-8")
    if old not in text:
        return {"ok": False, "error": "old string not found verbatim"}
    if text.count(old) > 1:
        return {"ok": False, "error": "old string is not unique"}
    p.write_text(text.replace(old, new, 1), encoding="utf-8")
    return {"ok": True, "path": path}


def list_dir(work_dir: str, path: str = ".") -> list[str]:
    p = _safe_path(work_dir, path)
    if not p.exists():
        return []
    return sorted(str(x.relative_to(Path(work_dir).resolve()))
                  for x in p.iterdir())


def run_bash(work_dir: str, command: str, timeout: int = 60) -> dict:
    lo = command.lower()
    for bad in _BASH_DENY:
        if bad in lo:
            return {"ok": False, "error": f"denied: {bad}"}
    for bad in _FORBIDDEN_BASH_FLAGS:
        if bad in command:
            return {"ok": False, "error": f"denied flag: {bad}"}
    try:
        proc = subprocess.run(
            ["bash", "-c", command], cwd=work_dir,
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"timeout after {timeout}s"}
    return {"ok": proc.returncode == 0, "exit": proc.returncode,
            "stdout": proc.stdout[-4000:], "stderr": proc.stderr[-2000:]}


def git_diff(work_dir: str) -> str:
    r = subprocess.run(["git", "-C", work_dir, "diff", "--stat", "HEAD"],
                        capture_output=True, text=True, timeout=10)
    return (r.stdout + r.stderr)[:3000]


def pytest_run(work_dir: str, args: str = "tests -q --tb=short") -> dict:
    # Always against vera-core
    target = os.path.join(work_dir, "vera-core")
    cmd = f"cd {shlex.quote(target)} && python -m pytest {args}"
    return run_bash(work_dir, cmd, timeout=180)
