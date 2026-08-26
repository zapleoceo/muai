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
from datetime import datetime, timezone
from pathlib import Path

# ─── Config ──────────────────────────────────────────────────────────────

HOME = Path(os.path.expanduser("~"))
CLAUDE_ROOT = HOME / ".claude" / "projects"
STATE_FILE = HOME / ".claude" / "vera_sync_state.json"
ENV_FILE = HOME / ".claude" / "vera_sync.env"



def _read_env_file(path: Path) -> dict[str, str]:
    """Прочитать KEY=VALUE. Скрипт САМ создаёт этот файл в --setup и до
    2026-08-26 никогда его не читал: секрет брался только из окружения,
    поэтому каждый запуск давал HTTP 401 и «85 errors, 0 posted». Порядок как
    у слушателя: окружение важнее файла."""
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.strip()] = value.strip()
    return out


_ENV = {**_read_env_file(ENV_FILE), **os.environ}
VERA_GATEWAY_URL = _ENV.get("VERA_GATEWAY_URL", "https://dima.veranda.my").rstrip("/")
INTERNAL_SECRET = _ENV.get("INTERNAL_SECRET", "")

# Records to skip entirely (UI / control plane, no semantic value)
SKIP_TYPES = {
    "custom-title", "ai-title", "mode", "queue-operation",
    "summary",   # autosummary record different from compact summary
}

# Hard cap per content text — keep events small
MAX_CONTENT_LEN = 16000

#: Cloudflare перед шлюзом режет запросы по подписи клиента: с
#: User-Agent «Python-urllib/3.12» он отдаёт 403 error code 1010 («banned
#: based on your browser signature»). Поймано вживую — синк не работал с
#: самого начала именно из-за этого, а curl проходил, потому что у него
#: другой UA. Своё имя честнее подделки под браузер и проходит.
USER_AGENT = "vera-claude-sync/1.0 (+https://dima.veranda.my)"


def _naive_utc(value: str | None) -> str | None:
    """Метка Claude («…Z», со смещением) → наивный UTC.

    Соглашение всего проекта — наивный UTC: `events.occurred_at` это
    `timestamp WITHOUT time zone`. Со зоной asyncpg падал DataError, а шлюз
    отдавал 500 (шлюз это теперь и сам нормализует, но врать ему незачем).
    """
    if not value:
        return value
    text = str(value).replace("Z", "+00:00")
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return value
    if moment.tzinfo is not None:
        moment = moment.astimezone(timezone.utc).replace(tzinfo=None)
    return moment.isoformat()



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
    timestamp = _naive_utc(rec.get("timestamp") or msg.get("timestamp"))
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
            "User-Agent": USER_AGENT,
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
            # Бинарно, а не текстом: у текстового файла f.tell() при итерации
            # запрещён («telling position disabled by next() call»), а смещение
            # нам нужно в БАЙТАХ — в тексте len(строки) считает символы, и на
            # кириллице курсор уехал бы вдвое. Считаем сами, tell не нужен.
            with jsonl.open("rb") as f:
                f.seek(offset)
                # Курсор двигается ТОЛЬКО за успешно отправленным. До 2026-08-26
                # он прыгал на размер файла «даже если отдельные события
                # упали» — и один 401 или обрыв сети молча съедал весь хвост
                # навсегда. Та же грабля, что чинили у gmail с date-granular
                # after: и у Trello: молча пропустить середину нельзя.
                good = position = offset
                for raw_bytes in f:
                    seen += 1
                    position += len(raw_bytes)
                    line = raw_bytes.decode("utf-8", errors="replace").strip()
                    if not line:
                        good = position
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        # Битая строка — это ядовитое сообщение, а не сбой
                        # транспорта: её пропускаем и идём дальше, иначе
                        # застрянем на ней навсегда.
                        good = position
                        continue
                    payload = parse_record(rec, project_dir.name, session_id)
                    if not payload:
                        good = position
                        continue
                    ok, info = post_event(payload)
                    if ok:
                        posted += 1
                        good = position
                        continue
                    errors += 1
                    logging.warning("отправка %s не прошла (%s) — курсор "
                                    "оставляю, хвост доберём в следующий раз",
                                    payload["source_event_id"], info)
                    break
                state[rel] = good
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

    # Сухой прогон НЕ трогает курсор. До 2026-08-26 он подменял отправку на
    # успех и сохранял состояние — то есть «посмотреть, что будет» молча
    # съедало весь бэклог: следующий настоящий прогон видел 216 строк вместо
    # 25 069, а 11 627 событий были помечены отправленными, ни разу не уйдя.
    if not args.dry_run:
        save_state(state)
    else:
        logging.info("dry-run: курсор не сдвинут")
    logging.info(
        "claude-sync done: %d files scanned, %d events seen, %d posted, %d errors, %.1fs",
        len(state), total_seen, total_posted, total_errors,
        time.time() - started,
    )


if __name__ == "__main__":
    main()
