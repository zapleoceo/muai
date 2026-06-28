# claude-ingestor

Syncs Claude Code transcripts (every project, every session) → Vera gateway.

## Why local

The JSONL transcripts only exist on Dima's laptop under
`~/.claude/projects/**/*.jsonl`. The server has no copy. Two choices:

- **Local sync (chosen)** — Python script runs in Windows Task Scheduler
  every 60 min, reads new lines from each JSONL, POSTs to gateway over
  HTTPS. Secrets stay in DB (encrypted volume); the raw transcript files
  never leave the laptop.
- Rsync — adds ~1h delay, leaves a full copy of the transcripts in
  `/var/www/vera3/` (plain text on disk).

## Setup (one-time)

```powershell
# On the laptop
cd D:\Projects\myAI\vera3\scripts
python claude_chat_sync.py --setup
# Edit ~/.claude/vera_sync.env — paste INTERNAL_SECRET from
# /var/www/vera3/infra/.env on hetzner-root

# Test
python claude_chat_sync.py --verbose

# Schedule (Task Scheduler GUI):
#   Trigger: Daily, repeat every 60 min for 24 hours
#   Action:  python.exe D:\Projects\myAI\vera3\scripts\claude_chat_sync.py
#   Settings: Run whether user logged on or not, no battery throttle
```

## State

- `~/.claude/vera_sync_state.json` — per-file byte offset, written
  atomically after each pass. Delete to re-sync everything.
- Gateway dedups by `source_event_id = "claude:{session_id}:{uuid}"`
  so re-runs are safe.

## What gets ingested

| JSONL `type` | Sent to Vera | Notes |
|---|---|---|
| `user` | yes | `author_role=self`, label `Я` |
| `assistant` | yes | `author_role=counterparty`, label `Claude` |
| `tool_use` | inline within assistant turn | `[tool_use: <name> <params>]` |
| `tool_result` | inline | `[tool_result] <first 1000 chars>` |
| `custom-title`, `ai-title`, `mode`, `queue-operation`, `summary` | NO | UI/control plane |

Compact summaries (`isCompactSummary=true`) are kept — they're the only
record of what happened before context reset.

## Failure modes

| Cause | Behavior |
|---|---|
| Gateway unreachable | logs error, offset NOT advanced for that file → retry next pass |
| Bad JSON line | skipped, offset still advances (don't loop) |
| Duplicate event | gateway returns 201 deduped — silent |
| `INTERNAL_SECRET` missing | script exits 1 immediately |

## Privacy note

Claude transcripts contain LLM keys, server commands, business data.
They're persisted to Vera's `events` table same as any other source —
encrypted volume, owner-only dashboard access via Telegram Login.
