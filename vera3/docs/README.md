# Vera 3.0 — documentation index

CI blocks pushes that change `vera3/services/**` or `vera3/shared/**`
without updating a file under `vera3/docs/`. Opt-out: put `docs-not-needed`
in the commit message (for cosmetic refactors only).

## Files

| Doc | Scope |
|---|---|
| [architecture.md](./architecture.md) | Services, data flow, event lifecycle |
| [llm-broker.md](./llm-broker.md) | How chat/embed route through AIbroker, fallback to local pool |
| [sources.md](./sources.md) | Each ingestor (telegram, gmail, instagram, vera_chat) — what it pulls, how, when |
| [brain.md](./brain.md) | Triage worker, agent loop, search synthesis, memory |
| [api.md](./api.md) | Gateway endpoints, dashboard routes |
| [deploy-ops.md](./deploy-ops.md) | rsync deploy, secrets, monitor, runbook |
| [domain-model.md](./domain-model.md) | Postgres schema (events, tokens, gmail_accounts, …) |
| [security.md](./security.md) | OAuth permanence, owner gate, encryption at rest |
| [conventions.md](./conventions.md) | File layout, async, types, comments |

## How to update

Every behavior change touches a doc. Update one of the above or write a
new one and link it here.
