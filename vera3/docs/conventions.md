# Conventions

## Python

- 3.12 baseline. `from __future__ import annotations` at the top of every file.
- Type hints on every public function. `X | None`, `list[X]`, `dict[K, V]`.
- Async everywhere for I/O. `asyncio.Lock` for shared mutable state.
- One responsibility per file. Hard ceiling ~200 lines, soft ~150.
- Layer order: routes → services → repository → models. No business logic
  in routes; no SQL outside the repository layer.

## Comments

- No comments that explain *what* — let names do that.
- Comments only for *why*: workaround, constraint, invariant, surprising
  behavior. Reference the trigger ("after the 2026-06-01 incident…") so
  future-you knows when to revisit.
- One-line docstrings or none.

## Errors

- Specific exception types with actionable messages.
- `log.warning` for expected transient (rate limits, network blips).
- `log.error` / `log.exception` only for unexpected.
- Never silently swallow.

## Naming

- `snake_case` for functions, variables, modules.
- `PascalCase` for classes.
- Private (module-internal) prefixed `_`.
- Service verbs: `get_`, `create_`, `update_`, `delete_`, `fetch_`, `record_`.

## Tests: SQLite for logic, Postgres for SQL

Unit tests run on `sqlite+aiosqlite` — fast, no service, and it is a *real*
database, which is why the dedup path and the schema are exercised against
it rather than a mock.

What it cannot do is run this project's SQL. Production is Postgres and the
hot paths are raw `text()`: `to_tsvector('russian')`, `FOR UPDATE SKIP
LOCKED`, `COUNT(*) FILTER (WHERE …)`, `metadata->>`, `= ANY(:ids)`, `NOW()`.
On SQLite most of those are a syntax or missing-function error, so the
bodies of `brain_triage.claim._claim_batch`, `dashboard.stats._compute_stats`
and `brain_search.search` were never executed by any test — coverage showed
0% across their lines, and the "test" for the claim query compared
`inspect.getsource()` against a string.

So: **anything that writes raw SQL needs a test in `tests/integration/`**,
which runs against a live Postgres (`pgvector/pgvector:pg16`, the production
image) started as a service container in both workflows. Those tests skip
themselves unless `RUN_INTEGRATION_TESTS=1` and `TEST_DATABASE_URL` are set,
so a local `pytest tests/` still works with no Postgres — but CI sets both,
and CI is the binding gate.

They must call the **production function**, not a copy of its query. The old
integration test inlined its own `UPDATE … SKIP LOCKED` that had drifted
from the real one (no `content_text != ''`, `ORDER BY id` instead of
`occurred_at DESC`, hardcoded `LIMIT 10`, `RETURNING id` instead of eleven
columns) — it proved that Postgres implements SKIP LOCKED, not that this
codebase uses it correctly.

Local Postgres for the same run:

```bash
docker run --rm -p 5433:5432 -e POSTGRES_PASSWORD=test pgvector/pgvector:pg16
RUN_INTEGRATION_TESTS=1 \
TEST_DATABASE_URL=postgresql+asyncpg://postgres:test@localhost:5433/postgres \
  pytest tests/integration
```

## Tests

- Mirror layout: `tests/unit/test_<module>.py` for a `src/.../<module>.py`.
- Pytest-asyncio mode "auto".
- In-memory SQLite for unit tests via `conftest.py` fixture.
- Integration tests in `tests/integration/` — they boot real services.
- Coverage gate currently **40%**, target 70%.

## SQLAlchemy

- `async with get_session() as s:` — every time.
- `session.execute(select(...))` — never legacy `session.query(...)`.
- Explicit commits via the `get_session` context manager.

## FastAPI

- Pydantic v2 for request/response models.
- Thin handlers: validate → call → return.
- Auth via `Depends(...)` — never reimplement in the body.

## Forbidden

- `from module import *`
- Mutable default arguments
- Bare `except:`
- Committing `.env`, `.session`, secrets in any form
- Comments in commit messages — write a real message
- TODO/FIXME with no owner or date

## Pre-commit hooks

`.pre-commit-config.yaml` is at the repo root. To enable locally:

```bash
pip install pre-commit
pre-commit install            # ruff + format + leak guards on commit
pre-commit install -t pre-push  # plus pytest unit run on push
```

Hooks: trailing whitespace, EOL fixer, YAML syntax, large-file guard,
merge-conflict markers, `detect-private-key` (blocks accidental .pem /
id_rsa commits), `ruff --fix` on `vera3/`, `ruff format` on `vera3/`,
`pytest -x` on push.

## Process

- One logical change per commit.
- Commit message starts with `feat:` / `fix:` / `refactor:` / `chore:` / `docs:` / `test:`.
- Push to `master` triggers Tests + Deploy + docs-check workflows. If
  docs-check blocks you, update the right `vera3/docs/*.md` file.
