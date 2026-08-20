# Vera 3

**Canonical project documentation: [`VERA.md`](VERA.md)** — orientation and
what is live. Detailed docs: [`vera3/docs/`](vera3/docs/). This file is a
stub kept for legacy tooling that expects a top-level `CLAUDE.md`.

## Quick facts

- Live URL: https://dima.veranda.my
- Server: Hetzner VPS, SSH alias `hetzner-root` (port 9617)
- Repo: https://github.com/zapleoceo/muai
- Project dir on server: `/var/www/vera3` (compose in `/var/www/vera3/infra`)
- Database: Postgres + pgvector — `docker exec vera3-postgres psql -U vera -d vera`
- Owner Telegram ID: `169510539`

Everything deployed lives under `vera3/`. The top-level `vera-core/`,
`vera-gmail/`, `vera-telegram/`, `vera-coder/`, `dashboard/`, `shared/`
directories and the root `docker-compose.yml` are **legacy v2 — not built,
not deployed**. See [`VERA.md`](VERA.md) §3.

See [VERA.md](VERA.md) for architecture, deploy flow, access model, known
gaps, and the migration log.

## Code conventions (binding)

Full version: [`vera3/docs/conventions.md`](vera3/docs/conventions.md).

- Python 3.12, `from __future__ import annotations`, async everywhere
- One file = one responsibility, ~200 line ceiling per file
- Type hints on every function signature, `X | None` not `Optional[X]`
- Layer order: routes → services → repository → models
- No business logic in routes; no SQL outside the repository layer
- Use `async with get_session()` — never reuse sessions across calls
- Always commit explicitly
- No bare `except:`, no swallowed exceptions
- No comments explaining *what* — names do that. Comments only for *why*

## Git

- Commit prefixes: `feat:`, `fix:`, `refactor:`, `chore:`, `docs:`, `test:`
- One logical change per commit
- Never commit `.env`, `*.session`, secrets of any kind
- Push to `master` runs the docs → tests → quality → deploy gates. A change
  under `vera3/services/` or `vera3/shared/` must come with a matching
  `vera3/docs/` change, or the docs gate blocks the deploy.
