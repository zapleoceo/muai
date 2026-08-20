#!/usr/bin/env bash
# vera-media-requeue.sh — drip recoverable media-recognition failures back into
# the media_pending queue, keeping it topped up to TARGET so the ~79.5k
# historical backlog (2026-07-19 audit) re-recognises in the background without
# ballooning media_pending. Voice/audio (fast whisper pool) go first, then
# newest photos. Live media is always claimed ahead of this (worker: id DESC).
# Safe on a cron — self-limiting, resumable, idempotent.
set -euo pipefail

TARGET="${VERA_MEDIA_QUEUE_TARGET:-800}"
psql() { docker exec vera3-postgres psql -U vera -d vera -tAc "$1" | tr -d '[:space:]'; }

# Шумные новостные каналы + большие публичные группы — фото из них НЕ
# распознаём (мемы/новости, ноль ценности для личной памяти Димы, жгут
# дефицитный vision-бюджет). Событие остаётся в КБ с плейсхолдером [photo].
NOISE_CHATS="
  AND COALESCE(metadata->>'chat_title','') NOT LIKE 'NEXTA Live%'
  AND COALESCE(metadata->>'chat_title','') NOT LIKE 'Українці%'
  AND COALESCE(metadata->>'chat_title','') NOT LIKE 'ХДніпро%'
  AND COALESCE(metadata->>'chat_title','') NOT LIKE 'ВЕЛИГАМНОСТЬ%'
  AND COALESCE(metadata->>'chat_title','') NOT LIKE 'Квизда%'
  AND COALESCE(metadata->>'chat_title','') NOT LIKE 'ИИ - БОТЫ%'
  AND COALESCE(metadata->>'chat_title','') NOT LIKE 'ChatGPT%'
  AND COALESCE(metadata->>'chat_title','') NOT LIKE 'Канал Лучкова%'
  AND COALESCE(metadata->>'chat_title','') NOT LIKE '%BadComedian%'"

RECOVERABLE_WHERE="metadata->>'media_recognition'='failed' AND triage_status='done'
  AND COALESCE(triage_error,'') NOT LIKE '%Could not find the input entity%'
  AND COALESCE(triage_error,'') NOT LIKE '%message not found%'
  AND COALESCE(triage_error,'') NOT LIKE '%too large%'
  AND COALESCE(triage_error,'') NOT LIKE '%413%'
  ${NOISE_CHATS}"

pending="$(psql "SELECT COUNT(*) FROM events WHERE triage_status='media_pending'")"
need=$(( TARGET - pending ))
if (( need <= 0 )); then
  echo "$(date -Is) media-requeue: queue=$pending >= target=$TARGET, nothing added"
  exit 0
fi

moved="$(psql "
WITH batch AS (
  SELECT id FROM events
  WHERE ${RECOVERABLE_WHERE}
  ORDER BY (metadata->>'media_kind' IN ('voice','audio')) DESC, occurred_at DESC
  LIMIT ${need}
), upd AS (
  UPDATE events e SET
    triage_status='media_pending', triage_error=NULL,
    metadata = (e.metadata - 'media_recognition' - 'media_retry_count' - 'media_next_retry_at')
  FROM batch WHERE e.id = batch.id
  RETURNING 1
)
SELECT COUNT(*) FROM upd")"

remaining="$(psql "SELECT COUNT(*) FROM events WHERE ${RECOVERABLE_WHERE}")"
echo "$(date -Is) media-requeue: queue was $pending, added $moved, backlog remaining $remaining"
