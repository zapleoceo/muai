# Entity dedup + relationship extraction

## The problem we had

2499 entity rows, only 2159 distinct names → ~340 duplicates (15 Alex,
12 Александр, 11 Сергей). 0 relationships. The graph layer existed but
nothing wrote to it.

Root cause: `entity_sync` upserts by `(source, sender_id)`. One person
talking via DM + 2 groups + 1 channel = 4 entities.

## Pieces

### Detection — `vera_shared.graph.dedup.find_duplicates_by_name`
Groups entity rows by case-folded, diacritics-stripped, ё→е/й→и normalized
name. Returns groups of ≥2 candidates ordered by size.

### Merge — `vera_shared.graph.dedup.merge_entities(keeper_id, merged_id)`
1. Move aliases that don't conflict on `(source, identifier)` UNIQUE → keeper
2. Drop remaining conflicting aliases on merged
3. Move memberships (both member side and group side)
4. Re-point relationships (subject + object)
5. Delete the merged entity row
Returns count of what moved. Idempotent on the uniqueness constraints.

### Owner UI — `/entities/duplicates`
Lists top-50 duplicate groups. Per group: candidates table (id, name,
alias count, recent 30d messages, membership count). Form: pick keeper +
merged → POST `/entities/merge` (303 redirect back).

Manual-by-design — auto-merge would be reckless given name collisions
("Алексей" the brother vs "Алексей" @mastermiks the colleague).

### Relationship extraction — `vera_shared.graph.rel_extract.extract_and_store`
Called from brain-triage after successful triage, only for events with
`importance >= 3`. Asks LLM (capability='structured', max 300 tokens) to
extract 0-3 tuples (subject, predicate, object, fact, confidence).

Predicates: `boss_of`, `reports_to`, `coworker_of`, `co_founder_of`,
`works_at`, `client_of`, `vendor_of`, `spouse_of`, `parent_of`,
`child_of`, `friend_of`, `lives_in`.

Resolution: entity name → `entities.name` exact, then
`entity_aliases.display_name`. Unknown name → skip (no auto-create).

Dedup: existing `(subject, predicate, object)` → bump `last_seen_at` +
`confidence = max(old, new)`. Else INSERT with `derived_from_event_id`
for audit/rollback.

Fire-and-forget — runs as `asyncio.create_task`, never blocks triage.

## Status: shipped 2026-06-28
