"""Env-configurable tuning constants for the triage worker."""
from __future__ import annotations

import os

from vera_shared.projects.rules import chat_id_canon_sql

POLL_INTERVAL_S = float(os.environ.get("TRIAGE_POLL_INTERVAL_S", "5"))
BATCH_SIZE = int(os.environ.get("TRIAGE_BATCH_SIZE", "16"))
CONCURRENCY = int(os.environ.get("TRIAGE_CONCURRENCY", "5"))
PACE_BETWEEN_S = float(os.environ.get("TRIAGE_PACE_S", "0.5"))
WORKER_ID = os.environ.get("HOSTNAME", "worker") + ":" + str(os.getpid())
# Сколько секунд триаж может работать прежде чем watchdog считает его мёртвым.
# Должно быть БОЛЬШЕ чем самый медленный LLM-вызов × CONCURRENCY.
STUCK_AFTER_S = int(os.environ.get("TRIAGE_STUCK_AFTER_S", "600"))

# ─── Групповой батчинг ───────────────────────────────────────────────────────
# Rate limiter (backfill_max_per_hour) считает LLM-ВЫЗОВЫ, не события. Группы
# (супергруппы + легаси Chat) — короткие сообщения (медиана ~260 симв.),
# батчим по TRIAGE_GROUP_BATCH_SIZE в ОДИН вызов → в N раз больше событий на
# тот же call-budget. Каналы (длиннее, медиана ~370, p99 ~2200) и личные чаты
# — НЕ батчим: канал — искажает контекст пачкой разнородных постов, личка —
# каждое сообщение Димы разбирается отдельно с полным вниманием модели.
TRIAGE_GROUP_BATCH_SIZE = int(os.environ.get("TRIAGE_GROUP_BATCH_SIZE", "10"))
# Safety valve: даже если чат числится "group", один аномально длинный текст
# не должен раздувать один LLM-вызов — сборка батча останавливается раньше
# TRIAGE_GROUP_BATCH_SIZE, если суммарный текст превысил это число символов.
TRIAGE_GROUP_BATCH_MAX_CHARS = int(os.environ.get("TRIAGE_GROUP_BATCH_MAX_CHARS", "6000"))

# Каноникализация chat_id (снимает -100-префикс супергрупп) для матча с
# project_membership. alias 'e' — таблица events в project_override.py.
CHAT_CANON = chat_id_canon_sql("e")
