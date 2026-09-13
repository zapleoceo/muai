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
`importance >= 60` that pass the data gate `rel_policy.rel_extract_skip_reason`
(see `brain.md`, rel-extract admission threshold). Asks LLM (capability='structured', max 300 tokens) to
extract 0-3 tuples (subject, predicate, object, fact, confidence).

Predicates: `boss_of`, `reports_to`, `coworker_of`, `co_founder_of`,
`works_at`, `client_of`, `vendor_of`, `spouse_of`, `parent_of`,
`child_of`, `friend_of`, `lives_in`.

Resolution: `repo.resolve_entity_exact(name)` — `entities.name` exact
(case-insensitive), then `entity_aliases.display_name`. Unknown name →
skip (no auto-create). This is the *exact* resolver; `find_entity_by_name`
is the separate *fuzzy* (`ILIKE %..%`) one used by the MCP entity lookup.

Dedup: the `(subject, predicate, object)` soft-upsert is
`repo.upsert_relationship` — the single source of truth (rel_extract used
to inline a near-identical raw-SQL copy that, unlike repo, never
back-filled a missing `fact`; that duplication was removed). Existing →
bump `last_seen_at` + `confidence = max(old, new)` (+ fill `fact` if it
was empty), returns False. Else INSERT with `derived_from_event_id` for
audit/rollback, returns True (so `extract_and_store` counts real inserts).

Background — spawned as a tracked `asyncio.create_task` held in a module
set (so the GC can't drop it mid-run), never blocks triage; runs *after*
the triage transaction commits.

### Validation before write — `vera_shared.graph.rel_validate` (2026-09-13)

Until 2026-09-03 structured calls were served mostly by mistral-small, and
it wrote a lot of junk that breaks on endpoint types: «Olga Kryachko /
Sintegrum works_at Olga Kryachko», «Ольга Крячко (JIRA) reports_to Vadim
Kudryavtsev», «Link works_at OpenRouter, Inc», «OpenRouter Team works_at
OpenRouter». Entity types that actually exist in `entities` (prod
2026-09-13): person 11519, organization 218, supergroup 56, bot 50, group
44, channel 27 — there is no «place» type.

`relationship_reject_reason(subject_name, subject_type, predicate,
object_name, object_type, confidence)` is a pure function, checked on the
*graph entities* (name + type), not on the model's strings — «Я» resolves
to the author, and only an entity has a type. Reasons:

- `low_confidence` — below `MIN_CONFIDENCE` 0.5;
- `not_referential` — `is_referential_name` (pronouns, one-letter scraps);
- `self_loop` — same id, or `same_person_name` after `normalized_name`
  («X / Org» -> X, «X (JIRA)» -> X) plus the translit/diminutive fold of
  `identity.canonical_name_parts`;
- `type_mismatch` — a chat (`bot`/`group`/`supergroup`/`channel`) on either
  end; `boss_of`/`reports_to`/`coworker_of`/`spouse_of`/`parent_of`/
  `child_of`/`friend_of` need person-person; `works_at`/`co_founder_of` need
  person -> organization; `client_of`/`vendor_of` person-or-organization on
  both ends; `lives_in` needs a person subject and a non-person object;
- `service_account` — `is_service_name`: «(JIRA)»-style tool tag or a
  service word (team, jira, noreply, support, `_bot`...) in either name.

Known limit: a structurally valid but semantically invented edge («Дима
friend_of Дарья | спасибо, Дарья») still passes — types can't catch it;
the data gate upstream is what reduces those.

### Quarantine of old junk — `scripts/quarantine_junk_rels.py`

Runs every current relationship through the same
`relationship_reject_reason` and marks failures `is_current = false` —
reversible, no DELETE. DB access lives in `graph/repo_relationships.py`
(`list_current_relationships_page`, `set_relationships_current`). The
script's `audit()` is read-only; `mark()` writes only after the report file
is saved; `report_ids()` with `--restore <report>` puts them back.

`is_current` semantics for relationships: it was always written as true
and only `list_relationships` (gateway entity card) filtered on it. Since
2026-09-13 `graph_snapshot` (neighbours, degrees, edges) and the avatar
queue degree (`avatars.list_entities_needing_avatar`) filter it too, so a
quarantined edge disappears from the card, the /graph page and degree
counts. `upsert_relationship` does not revive it (it only touches
`last_seen_at`), and the validator stops the same edge from being written
again. Merge (`dedup.merge_entities`) moves such rows like any other.

Dry-run on prod 2026-09-13 (read-only): 4406 current -> **2115 to mark**,
2291 stay. By reason: type_mismatch 1987, service_account 67, self_loop 56,
low_confidence 5. By predicate: works_at 1772, client_of 68, coworker_of 62,
lives_in 53, vendor_of 50, co_founder_of 47, reports_to 22, boss_of 15,
friend_of 15, spouse_of 8, parent_of 2, child_of 1. The bulk is «X works_at
<chat X is a member of>» (IT outsource 394, Українці у Вʼєтнамі 295) —
membership is already recorded in `memberships`.

```
docker compose run --rm --no-deps -v /var/www/vera3/scripts:/scripts \
  brain-triage python /scripts/quarantine_junk_rels.py --dry-run
... python /scripts/quarantine_junk_rels.py --report /scripts/junk_rels.json
... python /scripts/quarantine_junk_rels.py --restore /scripts/junk_rels.json
```

## Status: shipped 2026-06-28

## Bugfix 2026-06-28: real memberships schema

The first deploy used wrong column names (`member_entity_id`/`group_entity_id`).
Actual schema is `child_entity_id`/`parent_entity_id`. UNIQUE constraint is
`(parent_entity_id, child_entity_id, source)` — conflict-check fixed
accordingly.

## Honest note on auto-merge

Each "Alex"/"Сергей" duplicate group is N distinct Telegram user_ids, so
auto-merge by name alone is wrong. UNIQUE(source,sender_id) on aliases
already prevents the trivial case (same TG-id → same entity row).

Real auto-merge needs: phone overlap, @username overlap, OR embedding
similarity of message corpora > 0.85. None of those signals are present
in today's data — every alias is a unique TG-id. Manual review remains
the right path.
