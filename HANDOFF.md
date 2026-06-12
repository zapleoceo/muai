# HANDOFF — continuing Vera from another device

Quick-start context for any session opened from the cloud (claude.ai/code)
or phone. Read this first. Canonical project doc is [`VERA.md`](VERA.md);
this file only covers **how to keep working across devices**.

## TL;DR

- **Repo:** `zapleoceo/muai` · **Live:** https://dima.veranda.my
- **Deploy = `git push origin master`** → GitHub Action runs
  `scripts/deploy.sh` (build → up → smoke → pytest). Code changes reach
  prod by themselves, no SSH needed.
- Everything portable lives in **Git** (code + `VERA.md` + `.claude/`).
  Secrets and prod shell access do **not** travel — they wait for the
  trusted laptop.

## Works from cloud / phone

| Capability | Notes |
|---|---|
| Read/edit all code | Full repo + VERA.md + `.claude/rules` present |
| `git push` / `git pull` | Via the session's authorized proxy — no SSH key needed |
| Trigger deploy | Push to `master` → GH Action builds & deploys |
| Run unit tests | `cd vera-core && PYTHONPATH=../shared pytest -x` (SQLite, no prod creds) |

## Needs the laptop / SSH (not available from cloud)

| Capability | Why blocked |
|---|---|
| `ssh hetzner-root` (port 9617) | No SSH key in cloud env; port blocked by network policy |
| Prod DB ops (`sqlite3 /var/www/vera/data/vera.db`, backups) | Requires SSH |
| Manual `vera-deploy <svc>` on the box | Requires SSH (use the `git push` path instead) |
| Reading live logs / container state | Requires SSH |
| Anything needing `.env` secrets (Neo4j, tokens, SESSION_SECRET) | Secrets are not in the repo by design |

## Continuation checklist

1. **Before leaving the laptop:** commit + push everything
   (`git status` must be clean).
2. **From cloud/phone:** open `zapleoceo/muai`, say *"read HANDOFF.md"*,
   work on code, push to `master` (auto-deploy picks it up). For prod-DB
   or SSH steps, queue them as TODOs — they wait for the laptop.
3. **Back on the laptop:** `git pull`; run any deferred SSH/DB steps.

## Hard boundaries (do not violate)

- Never commit `.env`, `*.session`, or any secret (see
  `.claude/rules/security.md`).
- Never `docker compose down -v` in prod (destroys the DB volume).
- Admin routes stay `Depends(require_owner)`; deploy endpoint stays behind
  its Bearer secret.
