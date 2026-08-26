#!/usr/bin/env python
"""Sync Claude Code sessions (all projects) → Vera gateway, одной выжимкой.

Работает локально на ноутбуке (Windows Task Scheduler, раз в час). Читает
`~/.claude/projects/**/*.jsonl`, собирает сессию целиком и отправляет её на
`/v1/claude/session`; сервер осмысляет и пишет ОДНО событие на сессию.

До 2026-08-26 скрипт лил каждую реплику отдельным событием: одна рабочая
сессия давала сотни событий, набитых кодом и выводом команд, и в мозге тонуло
полезное. Нужна не переписка, а что делали, что решили и что осталось.

Почему локально: JSONL существуют только на ноутбуке. Rsync добавил бы час
задержки и выложил бы сырые расшифровки на файловую систему сервера.

Setup:
  $ python claude_chat_sync.py --setup    # шаблон конфига
  затем впиши INTERNAL_SECRET в ~/.claude/vera_sync.env

Запуск:
  $ python claude_chat_sync.py            # один проход, выход
  $ python claude_chat_sync.py --dry-run  # что бы отправил, ничего не меняя
  $ python claude_chat_sync.py --all      # не ждать, пока сессия остынет
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
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ─── Config ──────────────────────────────────────────────────────────────

HOME = Path(os.path.expanduser("~"))
CLAUDE_ROOT = HOME / ".claude" / "projects"
STATE_FILE = HOME / ".claude" / "vera_sync_state.json"
ENV_FILE = HOME / ".claude" / "vera_sync.env"

#: Сессию осмысляем, когда она остыла: живой файл дописывается, и выжимка
#: посреди работы устареет через минуту. Дописанную позже сессию догоним —
#: событие на сессию одно и обновляется.
QUIET_MINUTES = 120
#: Одна реплика без ответа — не сессия.
MIN_TURNS = 2
#: Потолок на реплику. Дампы `tool_result` бывают в десятки тысяч символов и
#: вытесняют смысл; свёртка на сервере всё равно режет текст на окна.
MAX_TURN_CHARS = 4000
#: Сколько ждать, пока воркер осмыслит принятые сессии. Не дождались — курсор
#: не двинулся, доберём в следующий проход.
DEFAULT_WAIT_S = 3600.0
POLL_EVERY_S = 20.0

STATE_VERSION = 2


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

#: Служебные записи интерфейса — смысла не несут.
SKIP_TYPES = {"custom-title", "ai-title", "mode", "queue-operation", "summary"}

#: Cloudflare перед шлюзом режет запросы по подписи клиента: с
#: User-Agent «Python-urllib/3.12» он отдаёт 403 error code 1010 («banned
#: based on your browser signature»). Поймано вживую — синк не работал с
#: самого начала именно из-за этого, а curl проходил, потому что у него
#: другой UA. Своё имя честнее подделки под браузер и проходит.
USER_AGENT = "vera-claude-sync/2.0 (+https://dima.veranda.my)"


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


# ─── Состояние: сколько реплик сессии уже осмыслено ──────────────────────


def load_state() -> dict[str, int]:
    """Версия 1 хранила БАЙТОВОЕ смещение по тем же ключам.

    Прочитать её как число реплик нельзя: смещение 480000 значило бы «уже
    осмыслено 480000 реплик», и сессия не отправилась бы никогда. Старое
    состояние поэтому не переносим — сессии соберутся заново, а событие на
    сессию одно, так что дубли исключены конструкцией.
    """
    if not STATE_FILE.exists():
        return {}
    try:
        raw = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logging.warning("состояние битое, начинаю заново")
        return {}
    if not isinstance(raw, dict) or raw.get("version") != STATE_VERSION:
        logging.info("состояние старой версии — сессии соберутся заново")
        return {}
    return dict(raw.get("sessions") or {})


def save_state(sessions: dict[str, int]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"version": STATE_VERSION, "sessions": sessions},
                              indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


# ─── Разбор JSONL ────────────────────────────────────────────────────────


def _extract_text(content: object) -> str:
    """Content у Claude — строка ИЛИ список блоков."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            parts.append(block.get("text", ""))
        elif kind == "tool_use":
            name = block.get("name", "?")
            params = json.dumps(block.get("input", {}), ensure_ascii=False)[:500]
            parts.append(f"[инструмент: {name} {params}]")
        elif kind == "tool_result":
            result = block.get("content", "")
            if isinstance(result, list):
                result = " ".join(x.get("text", "") for x in result
                                  if isinstance(x, dict))
            parts.append(f"[результат] {str(result)[:1000]}")
    return "\n".join(p for p in parts if p)


def read_session(path: Path) -> dict | None:
    """Файл сессии → payload для шлюза, либо None если отправлять нечего.

    Сайдчейны (`isSidechain`) пропускаем: это внутренний диалог сабагентов, он
    в разы длиннее основной ветки, а результат делегирования всё равно
    приходит в основную ветку ответом агента.
    """
    turns: list[dict[str, str]] = []
    first_ts: str | None = None
    last_ts: str | None = None
    cwd: str | None = None
    branch: str | None = None

    for raw in path.read_bytes().splitlines():
        line = raw.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            # Битая строка — ядовитое сообщение, а не сбой транспорта.
            continue
        if record.get("type") in SKIP_TYPES or record.get("isSidechain"):
            continue
        if record.get("type") not in {"user", "assistant"}:
            continue
        message = record.get("message") or {}
        text = _extract_text(message.get("content", "")).strip()
        if not text:
            continue
        role = message.get("role") or record.get("type")
        turns.append({"role": "user" if role == "user" else "assistant",
                      "text": text[:MAX_TURN_CHARS]})
        stamp = _naive_utc(record.get("timestamp") or message.get("timestamp"))
        if stamp:
            first_ts = first_ts or stamp
            last_ts = stamp
        cwd = record.get("cwd") or cwd
        branch = record.get("gitBranch") or branch

    if len(turns) < MIN_TURNS:
        return None
    fallback = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
    fallback_iso = fallback.replace(tzinfo=None).isoformat()
    return {
        "session_id": path.stem,
        "project_dir": path.parent.name,
        "started_at": first_ts or fallback_iso,
        "ended_at": last_ts or fallback_iso,
        "cwd": cwd,
        "git_branch": branch,
        "turns": turns,
    }


# ─── Отправка ────────────────────────────────────────────────────────────


def _call(url: str, *, data: bytes | None = None) -> tuple[bool, dict | str]:
    request = urllib.request.Request(
        url, data=data, method="POST" if data else "GET",
        headers={
            "Content-Type": "application/json",
            "X-Internal-Secret": INTERNAL_SECRET,
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            body = response.read().decode("utf-8", errors="replace") or "{}"
            return (200 <= response.status < 300, json.loads(body))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="ignore")[:200]
        return (False, f"HTTP {e.code}: {detail}")
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
        return (False, f"{type(e).__name__}: {e}")


def enqueue(payload: dict) -> tuple[bool, str]:
    """Отдать сессию шлюзу. Он только принимает — осмысляет фоновый воркер.

    Осмыслить в самом запросе нельзя, и это измерено: одно окно на 21 тыс.
    символов не уложилось в 120с ожидания брокера, а nginx обрывает `/v1/` по
    дефолтным 60с — синхронная версия ловила 504 ровно на 60.8-й секунде.
    """
    ok, body = _call(f"{VERA_GATEWAY_URL}/v1/claude/session",
                     data=json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    if not ok:
        return False, str(body)
    return True, str(body.get("status") or "?") if isinstance(body, dict) else "?"


def session_state(session_id: str) -> tuple[str, int, str]:
    """(статус, осмыслено реплик, пояснение)."""
    ok, body = _call(f"{VERA_GATEWAY_URL}/v1/claude/session/{session_id}")
    if not ok or not isinstance(body, dict):
        return "unknown", 0, str(body)
    return (str(body.get("status") or "unknown"),
            int(body.get("done_turns") or 0),
            str(body.get("error") or ""))


def sync(state: dict[str, int], pending: dict[str, tuple[str, int]], *,
         only_quiet: bool, dry_run: bool) -> tuple[int, int, int, int]:
    """Отдать шлюзу всё новое. (увидел, принято, пропущено, ошибок)."""
    seen = sent = skipped = errors = 0
    quiet_before = datetime.now(timezone.utc) - timedelta(minutes=QUIET_MINUTES)

    for path in sorted(CLAUDE_ROOT.rglob("*.jsonl")):
        key = str(path.relative_to(CLAUDE_ROOT))
        try:
            modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        except OSError:
            continue
        seen += 1
        if only_quiet and modified > quiet_before:
            skipped += 1
            logging.debug("%s ещё пишется — позже", key)
            continue
        try:
            payload = read_session(path)
        except OSError as e:
            logging.warning("%s не прочитался: %s", key, e)
            errors += 1
            continue
        if payload is None:
            skipped += 1
            continue
        turns = len(payload["turns"])
        if state.get(key, 0) >= turns:
            skipped += 1
            continue
        if dry_run:
            logging.info("[dry-run] %s: реплик %d, проект %s",
                         key, turns, payload["project_dir"])
            sent += 1
            continue
        ok, info = enqueue(payload)
        if not ok:
            errors += 1
            logging.warning("%s не принят (%s) — повторим в следующий раз",
                            key, info)
            continue
        sent += 1
        if info == "done":
            # Шлюз ответил, что в этом объёме сессия уже осмыслена.
            state[key] = turns
        else:
            pending[key] = (payload["session_id"], turns)
        logging.info("%s: реплик %d, статус %s", key, turns, info)
    return seen, sent, skipped, errors


def await_queue(state: dict[str, int], pending: dict[str, tuple[str, int]], *,
                deadline_s: float) -> tuple[int, int]:
    """Дождаться, пока воркер осмыслит принятое. (осмыслено, осталось).

    Курсор двигается ТОЛЬКО за реально осмысленным: иначе обрыв или ядовитая
    сессия молча выпадут навсегда — та же грабля, что чинили у gmail и trello.
    """
    done = 0
    until = time.time() + deadline_s
    while pending and time.time() < until:
        time.sleep(POLL_EVERY_S)
        for key in list(pending):
            session_id, turns = pending[key]
            status, done_turns, note = session_state(session_id)
            if status == "done" and done_turns >= turns:
                state[key] = turns
                pending.pop(key)
                done += 1
                logging.info("%s осмыслена (реплик %d)", key, turns)
            elif status in {"error", "unknown"}:
                pending.pop(key)
                logging.warning("%s не осмыслена (%s %s)", key, status, note)
    for key in pending:
        logging.info("%s ещё в очереди — курсор не двигаю", key)
    return done, len(pending)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--setup", action="store_true", help="шаблон конфига")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--dry-run", action="store_true",
                        help="показать, что отправил бы; ничего не менять")
    parser.add_argument("--all", action="store_true",
                        help="не ждать, пока сессия остынет")
    parser.add_argument("--wait", type=float, default=DEFAULT_WAIT_S,
                        metavar="СЕК",
                        help="сколько ждать осмысления принятых сессий")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if args.setup:
        ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
        ENV_FILE.write_text(
            "VERA_GATEWAY_URL=https://dima.veranda.my\n"
            "INTERNAL_SECRET=<взять из /var/www/vera3/infra/.env на hetzner-root>\n",
            encoding="utf-8",
        )
        print(f"Создал {ENV_FILE} — впиши INTERNAL_SECRET и запусти без --setup")
        return

    if not INTERNAL_SECRET:
        print(f"INTERNAL_SECRET пуст. Запусти --setup и заполни {ENV_FILE}.",
              file=sys.stderr)
        sys.exit(1)
    if not CLAUDE_ROOT.exists():
        print(f"Нет {CLAUDE_ROOT}", file=sys.stderr)
        sys.exit(1)

    state = load_state()
    pending: dict[str, tuple[str, int]] = {}
    started = time.time()
    seen, sent, skipped, errors = sync(state, pending, only_quiet=not args.all,
                                       dry_run=args.dry_run)
    ready = 0
    if pending:
        logging.info("принято в очередь %d — ждём осмысления до %.0f мин",
                     len(pending), args.wait / 60)
        ready, _left = await_queue(state, pending, deadline_s=args.wait)
    # Состояние сохраняем только после реальной отправки: до 2026-08-26
    # --dry-run подменял отправку успехом И сохранял состояние, то есть
    # «холостой» прогон съедал всю историю навсегда.
    if not args.dry_run:
        save_state(state)
    logging.info("claude-sync: сессий %d, принято %d, осмыслено %d, "
                 "пропущено %d, ошибок %d, %.1fс", seen, sent, ready, skipped,
                 errors, time.time() - started)
    sys.exit(1 if errors and not sent else 0)


if __name__ == "__main__":
    main()
