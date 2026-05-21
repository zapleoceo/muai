# Vera 2.0

**Canonical project documentation: [`VERA.md`](VERA.md)** — that is the single
source of truth. This file is a stub kept for legacy tooling that expects
a top-level `CLAUDE.md`.

## Quick facts

- Live URL: https://dima.veranda.my
- Server: Hetzner VPS, SSH alias `hetzner-root` (port 9617)
- Repo: https://github.com/zapleoceo/muai
- Project dir on server: `/var/www/vera`
- Owner Telegram ID: `169510539`

See [VERA.md](VERA.md) for architecture, conventions, security model,
domain models, deploy flow, and migration log.

## Code conventions (binding)

- Python 3.12, async everywhere
- One file = one responsibility, ~200 line ceiling per file
- Type hints on every function signature, `X | None` not `Optional[X]`
- Layer order: routes → services → repository → models
- No business logic in routes; no DB access outside repository layer
- Use `async with get_session()` — never reuse sessions across calls
- Always commit explicitly
- No bare `except:`, no swallowed exceptions
- No comments explaining *what* — names do that. Comments only for *why*

## Git

- Commit prefixes: `feat:`, `fix:`, `refactor:`, `chore:`
- One logical change per commit
- Never commit `.env`, `*.session`, secrets of any kind
