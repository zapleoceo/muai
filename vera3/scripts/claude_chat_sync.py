#!/usr/bin/env python
"""Sync Claude Code transcripts (all sessions, all projects) → Vera gateway.

Runs locally on Dima's laptop (Windows Task Scheduler, every 60 min).
Reads ~/.claude/projects/**/*.jsonl, POSTs new events to Vera gateway
over HTTPS. State (last byte offset per file) kept in a small local
JSON next to the script.

Why local: the JSONL files only exist on the laptop. Rsync would add
~1h delay + leaks the raw transcripts to the server's filesystem. POSTing
event-by-event keeps secrets only in DB (encrypted volume).

Setup:
  $ python claude_chat_sync.py --setup    # writes config template
  Then edit ~/.claude/vera_sync.env with VERA_GATEWAY_URL + INTERNAL_SECRET
  Then add Task Scheduler trigger: every 60 min, run this script.

Run manually:
  $ python claude_chat_sync.py            # one sync pass, exits
  $ python claude_chat_sync.py --verbose  # logs every file/event

Idempotent: source_event_id = "claude:{session_id}:{message_uuid}".
Gateway dedups; safe to re-run with state file deleted.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# ─── Config ──────────────────────────────────────────────────────────────

HOME = Path(os.path.expanduser("~"))
CLAUDE_ROOT = HOME / ".claude" / "projects"
STATE_FILE = HOME / ".claude" / "vera_sync_state.json"
ENV_FILE = HOME / ".claude" / "vera_sync.env"

VERA_GATEWAY_URL = os.environ.get("VERA_GATEWAY_URL", "https://dima.veranda.my")
INTERNAL_SECRET = os.environ.get("INTERNAL_SECRET", "")

# Records to skip entirely (UI / control plane, no semantic value)
SKIP_TYPES = {
    "custom-title", "ai-title", "mode", "queue-operation",
    "summary",   # autosummary record different from compact summary
}

# Hard cap per content text — keep events small
MAX_CONTENT_LEN = 16000


# ─── State (per-file byte offset) ────────────────────────────────────────


def load_state() -> dict[str, int]:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        logging.warning("state file corrupt, starting fresh")
        return {}


def save_state(state: dict[str, int]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


# ─── JSONL parsing ───────────────────────────────────────────────────────


def _extract_text(content) -> str:
    """Claude messages have content as str OR list[block]."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if not isinstance(b, dict):
                continue
            t = b.get("type")
            if t == "text":
                parts.append(b.get("text", ""))
            elif t == "tool_use":
                name = b.get("name", "?")
                params = json.dumps(b.get("input", {}), ensure_ascii=False)[:500]
                parts.append(f"[tool_use: {name} {params}]")
            elif t == "tool_result":
                result = b.get("content", "")
                if isinstance(result, list):
                    result = " ".join(
                        x.get("text", "") for x in result if isinstance(x, dict)
                    )
                parts.append(f"[tool_result] {str(result)[:1000]}")
        return "\n".join(p for p in parts if p)
    return ""


def parse_record(rec: dict, project_dir: str, session_id: str) -> dict | None:
    """Convert one JSONL line → event payload, or None to skip."""
    rec_type = rec.get("type")
    if rec_type in SKIP_TYPES:
        return None
    if rec_type not in {"user", "assistant"}:
        return None

    msg = rec.get("message") or {}
    role = msg.get("role") or rec_type
    text = _extract_text(msg.get("content", ""))
    if not text.strip():
        return None

    uuid = rec.get("uuid") or rec.get("id")
    if not uuid:
        return None
    timestamp = rec.get("timestamp") or msg.get("timestamp")
    cwd = rec.get("cwd", "")
    git_branch = rec.get("gitBranch", "")
    is_compact_summary = bool(rec.get("isCompactSummary"))

    author_role = "self" if role == "user" else "counterparty"
    author_label = "Я" if author_role == "self" else "Claude"

    body = text[:MAX_CONTENT_LEN]
    content_text = (
        f"Author: {author_label} [{author_role}]\n"
        f"Project: {project_dir}\n"
        f"Session: {session_id}\n"
        f"Role: {role}\n"
        f"Date: {timestamp or ''}\n"
        f"{'(compact summary)' if is_compact_summary else ''}\n"
        f"---\n{body}"
    )

    return {
        "source": "claude_chat",
        "source_event_id": f"claude:{session_id}:{uuid}",
        "category": role,
        "content_text": content_text,
        "occurred_at": timestamp,
        "metadata": {
            "author_role": author_role,
            "author_label": author_label,
            "project_dir": project_dir,
            "session_id": session_id,
            "uuid": uuid,
            "role": role,
            "cwd": cwd,
            "git_branch": git_branch,
            "is_compact_summary": is_compact_summary,
            "model": msg.get("model"),
            "is_sidechain": rec.get("isSidechain"),
            "entrypoint": rec.get("entrypoint"),
        },
    }


# ─── Gateway POST ────────────────────────────────────────────────────────


def post_event(payload: dict) -> tuple[bool, str]:
    url = f"{VERA_GATEWAY_URL}/event/claude_chat"
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Internal-Secret": INTERNAL_SECRET,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return (200 <= r.status < 300, f"HTTP {r.status}")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")[:200]
        return (False, f"HTTP {e.code}: {body}")
    except Exception as e:
        return (False, f"{type(e).__name__}: {e}")


# ─── Main scan ───────────────────────────────────────────────────────────


def scan_project(project_dir: Path, state: dict[str, int],
                 verbose: bool) -> tuple[int, int, int]:
    """Returns (events_seen, events_posted, errors)."""
    seen = posted = errors = 0
    for jsonl in project_dir.rglob("*.jsonl"):
        rel = str(jsonl.relative_to(CLAUDE_ROOT))
        offset = state.get(rel, 0)
        try:
            size = jsonl.stat().st_size
        except OSError:
            continue
        if size <= offset:
            continue   # nothing new

        session_id = jsonl.stem
        try:
            with jsonl.open("r", encoding="utf-8", errors="replace") as f:
                f.seek(offset)
                for raw_line in f:
                    seen += 1
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    payload = parse_record(rec, project_dir.name, session_id)
                    if not payload:
                        continue
                    ok, info = post_event(payload)
                    if ok:
                        posted += 1
                    else:
                        errors += 1
                        if verbose:
                            logging.warning("POST fail %s: %s",
                                            payload["source_event_id"], info)
                # Update offset to current file size — even if individual events
                # failed, we don't loop forever on bad lines.
                state[rel] = f.tell()
        except Exception as e:
            logging.exception("scan %s failed: %s", rel, e)
            errors += 1
            continue
    return seen, posted, errors


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--setup", action="store_true",
                        help="Write env template and exit")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--dry-run", action="store_true",
                        help="Parse and count, do NOT post")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if args.setup:
        ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
        ENV_FILE.write_text(
            "VERA_GATEWAY_URL=https://dima.veranda.my\n"
            "INTERNAL_SECRET=<paste-from-server-/var/www/vera3/infra/.env>\n",
            encoding="utf-8",
        )
        print(f"Wrote {ENV_FILE} — fill INTERNAL_SECRET then re-run without --setup")
        return

    # Load env from file (Windows Task Scheduler doesn't pass env nicely)
    global VERA_GATEWAY_URL, INTERNAL_SECRET
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k == "VERA_GATEWAY_URL":
                VERA_GATEWAY_URL = v.strip()
            elif k == "INTERNAL_SECRET":
                INTERNAL_SECRET = v.strip()

    if not INTERNAL_SECRET:
        print("ERROR: INTERNAL_SECRET not set. Run --setup, then edit env file.",
              file=sys.stderr)
        sys.exit(1)

    if not CLAUDE_ROOT.exists():
        print(f"ERROR: {CLAUDE_ROOT} not found", file=sys.stderr)
        sys.exit(1)

    if args.dry_run:
        global post_event   # type: ignore
        def _noop(p): return (True, "dry-run")  # noqa
        post_event = _noop   # type: ignore

    state = load_state()
    started = time.time()
    total_seen = total_posted = total_errors = 0

    for project in sorted(CLAUDE_ROOT.iterdir()):
        if not project.is_dir():
            continue
        seen, posted, errors = scan_project(project, state, args.verbose)
        if posted or errors:
            logging.info("project %s: seen=%d posted=%d errors=%d",
                         project.name, seen, posted, errors)
        total_seen += seen
        total_posted += posted
        total_errors += errors

    save_state(state)
    logging.info(
        "claude-sync done: %d files scanned, %d events seen, %d posted, %d errors, %.1fs",
        len(state), total_seen, total_posted, total_errors,
        time.time() - started,
    )


if __name__ == "__main__":
    main()
