# Python rules — myAI project

Apply these rules to every Python file in this project.

## Structure
- One responsibility per file. Max ~200 lines; split if larger.
- Layer order: `api/` → `services/` → `repository.py` → `models.py`
- No business logic in routes; no DB access outside repository layer.
- Singletons: module-level `_var: Type | None = None` + `get_var() -> Type`.

## Async
- All DB, HTTP, and file I/O must be `async`.
- Use `asyncio.Lock` for shared mutable state (see `TokenManager`).
- Never `asyncio.sleep` in a polling loop — diagnose and fix the root cause.

## Types
- Type hints on every function signature.
- Use `X | None` not `Optional[X]`.
- Use `list[X]` / `dict[K, V]` not `List` / `Dict`.

## Comments & docs
- No comments that explain *what* code does — names do that.
- Add a comment only when *why* is non-obvious (workaround, constraint, invariant).
- No docstrings longer than one line. Prefer none.

## Error handling
- Raise specific exceptions with clear messages.
- Log at `WARNING` for expected transient failures (rate limits, timeouts).
- Log at `ERROR`/`EXCEPTION` only for unexpected failures.
- Never swallow exceptions silently.

## SQLAlchemy
- Always use `async with AsyncSessionLocal() as session:` — never reuse sessions across calls.
- Use `session.execute(select(...))` not legacy `session.query(...)`.
- Commit explicitly; never rely on implicit commit.

## FastAPI routes
- Thin: validate input → call service → return result.
- Use `Depends(require_owner)` for all admin routes.
- Return plain `dict` or Pydantic model; no ORM objects directly.

## Naming
- `snake_case` for functions, variables, modules.
- `PascalCase` for classes.
- Prefix private/module-internal with `_`.
- Services: verb phrases (`get_`, `create_`, `update_`, `delete_`).

## Forbidden
- `from module import *`
- Mutable default arguments (`def f(x=[])`)
- Bare `except:` without specifying exception type
- Committing `.env`, session files, or secrets of any kind
