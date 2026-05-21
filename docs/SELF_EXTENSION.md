# Vera self-extension — proposal (no code yet)

> Discussion document. Implementation gated on Dima's explicit go-ahead.
> Canonical project doc: [`/VERA.md`](../VERA.md)

## Goal

Vera grows new capabilities **without code changes** — she discovers MCP
servers, asks Dima for credentials when needed, and installs them. She never
writes her own service code, never deploys without owner approval, never
adds tools she cannot describe and explain.

## Non-goals (hard)

- ❌ Autonomous code generation for new services (vera-instagram, vera-bank…)
- ❌ Autonomous git push / docker compose changes
- ❌ Installing arbitrary npm/pypi packages whose origin isn't whitelisted
- ❌ Storing credentials Vera received from a tool output (only from Dima)

## Trust ladder

Each capability lives at exactly one of these tiers:

| Tier | Action | Trigger |
|---|---|---|
| **T0 — already wired** | Use existing tool | LLM picks it, server resolves destructive args |
| **T1 — discover** | Search MCP registry for a tool matching need | Vera detects "I needed X but have no tool" |
| **T2 — propose** | Suggest one specific package + describe what it would do | Confidence in match ≥ 0.7 |
| **T3 — gather creds** | Ask Dima in DM for required env vars | Owner explicit "Yes, install" |
| **T4 — install** | Insert row into `mcp_servers`, refresh manager | Cred input complete + retry budget < 3 |
| **T5 — verify** | After start: call `list_tools`, run one safe read-only tool | Auto, with rollback on error |

Crossing each line **must** be a separate user-visible step. No "Vera silently
discovered and installed X" — every install is a paper trail in DM + dashboard.

## Discovery flow (concrete)

Trigger: Vera in a triage step thinks *"to handle this I'd need an Instagram-DM
tool, but my registry has none."*

```
1. Vera (still inside the triage LLM call):
     emits a structured "capability_gap" record:
       {needed: "send instagram DM", source_event: 42, blocking: true}

2. Dispatcher catches capability_gap → spawns `discovery` worker:
     a) calls `mcp_registry.search("instagram dm")` (MCP we already have)
     b) ranks results by stars, last_publish, official-vendor flag,
        existing-install-in-other-Vera-instances
     c) picks top 1 with score ≥ threshold

3. Worker DMs Dima:
     ✨ Чтобы обработать событие #42 (комментарий от @user в Instagram)
     мне нужен инструмент которого у меня нет.
     Нашла в реестре:  @pinkpixel/instagram-engagement-mcp
     (★ 142, обновлён 3 нед назад, MIT, github.com/...).
     Он добавит инструменты: instagram_send_dm, instagram_list_comments, ...
     Нужны credentials:
       - INSTAGRAM_ACCESS_TOKEN
       - INSTAGRAM_BUSINESS_ACCOUNT_ID
     [📦 Поставить] [❌ Не надо] [🔍 Покажи альтернативы]

4. Dima taps "Поставить":
     Bot DM: "Пришли INSTAGRAM_ACCESS_TOKEN reply'ем"
     [waits, captures via reply-to-message]
     Bot DM: "OK. Теперь INSTAGRAM_BUSINESS_ACCOUNT_ID"
     [waits]

5. Worker:
     a) inserts mcp_servers row (enabled=true)
     b) manager.refresh_from_db() — spawns subprocess
     c) reads tools_count; if 0 → rollback row, DM "не запустилось: <err>"
     d) calls one safe read tool (e.g. instagram_get_profile) — must return 200
     e) DM "✅ Готово. Добавила 12 инструментов. Возвращаюсь к событию #42"

6. Triage retries with new tool registry — proceeds normally.
```

## Guardrails

| What | How |
|---|---|
| Only whitelisted registries | `mcp_registry` MCP queries a single npm/pypi index. No arbitrary URLs. |
| Cred entry is reply-to-message only | Same mechanism as the «Свой ответ» followup. No webhook intake. |
| Creds never logged | TG message text never goes to INFO logs; stored encrypted via `crypto.py` |
| Install rate-limit | Max 1 install per hour, 5 per day per source-need |
| Test-on-install | If first tool call fails, automatic rollback and DM error |
| Audit | Every install is an `events` row with `source='self_extension'` and full chain |
| Owner sees pending installs in dashboard | New tab «Self-extend» with history + pending state |

## Non-MCP path (for cases without a community server)

If no MCP exists for a need, Vera DMs:

> *"Не нашла готового MCP для X. Могу:*
> 1. *Записать пожелание в backlog и попросить тебя написать самой*
> 2. *Открыть GitHub issue в нашем репо с описанием*
> 3. *Пропустить — событие останется в state «no-tool»"*

She does NOT generate code, does NOT modify docker-compose.yml, does NOT
push to git. The line between "configure what exists" and "extend the system"
stays on the human side.

## What needs to be built (when we say go)

| | LoC | Risk |
|---|---|---|
| `app/self_extension/discovery.py` — gap detector | ~80 | low |
| `app/self_extension/proposer.py` — DM flow + reply capture | ~120 | medium (touches bot handler) |
| `app/self_extension/installer.py` — DB insert + refresh + test call | ~70 | low |
| Dashboard tab «Self-extend» | ~150 | low |
| End-to-end test with mocked MCP registry | ~80 | low |

Estimated total: 1 working day.

## Open questions for Dima

1. **Single registry vs multi?** I propose only npm + the curated `mcp-registry` MCP.
2. **Daily install budget?** I default to 5/day. Too generous?
3. **Self-removal:** if Vera notices a tool never used in 30 days, should she
   propose uninstall? Or only Dima removes?
4. **Cred refresh:** when a token expires, Vera DMs to refresh, or fails
   silently and asks at next gap-event?

Implementation is gated on Dima saying "go" + answers above.
