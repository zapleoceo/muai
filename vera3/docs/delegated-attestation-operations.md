# Delegated attestation: operational review package

Status: draft PR only. Migration 056 and policy activation require separate approval. This document describes the exact scoped change; it is not an instruction to apply it automatically.

## Scope and trust

Only one-word literal text replies from `help-vc-*` voice commands with the matching Vera authorship question can be attested. The OAuth principal is `dot`; the resulting answer is `dot-attested`, never `OWNER`. An evidence message ID, timestamp, quote, exact task ID, qid and Office revision are required. The code phrase alone is not evidence of identity. The system records dot's assertion about an owner message; it cannot independently inspect that chat. No permission, table, microphone or Listener configuration change is included.

The instruction parser accepts either a bare one-word reply or that same word
inside paired `«...»` quotation marks. Text inside the marks must still be one
literal word. A second clause, requested tool action, data retrieval or expense
remains outside this attestation scope.

The task is left with `auto_pickup=false`. Dot explicitly claims it and records an existing send receipt with `room_task_attested_complete`; the tool does not send text. The same receipt replays without a second done event and cannot be bound to another task through this tool. Generic task completion, mutation and task-scoped room posting are rejected after attestation. Unscoped `room_post` remains a general OAuth capability and is outside this task workflow.

## Migration 056, separate approval

The only schema change widens `ck_room_task_events_kind` on `room_task_events` from 19 to 20 values, adding `delegated_attestation`. There is no new table or deletion. Phase 1 is one transaction: `SET LOCAL lock_timeout = '5s'`, drop the old CHECK and add the widened CHECK `NOT VALID`. It requests `AccessExclusiveLock`, briefly blocks reads and writes while held, and aborts rather than waits indefinitely. Phase 2 is a separate transaction: `SET LOCAL lock_timeout = '5s'`, `VALIDATE CONSTRAINT ck_room_task_events_kind`, then insert the `056_delegated_attestation_event` migration marker. Validation uses `ShareUpdateExclusiveLock`; normal DML can continue, but conflicting schema operations cannot. PostgreSQL integration tests measured these lock modes and exercised the timeout path.

Preflight on the actual target: verify current master/deployed commit, schema migration ledger through 055, current CHECK definition, no long-running transactions or conflicting DDL, recent restorable backup and disk headroom. The repository migration runner is `vera3/scripts/apply_migration.sh vera3/infra/migrations/056_delegated_attestation_event.sql` from the deployed repository root. Run it only after explicit approval of migration 056. Verify the migration marker and `pg_constraint.convalidated=true`, and confirm the CHECK includes all 20 expected values. Do not enable the flag if either phase is incomplete.

If phase 1 fails, its transaction rolls back to the 19-value CHECK. If phase 2 fails, phase 1's widened `NOT VALID` CHECK remains and still enforces future writes; leave the feature flag off, resolve the blocker, and rerun validation and marker insertion. Never drop attestation audit events or narrow the CHECK after any attestation event exists. A safe application rollback is `ROOM_ATTEST_ENABLED=0`, preserving events and the widened CHECK.

## Activation, separate approval

After migration verification and reviewed code deployment, set `ROOM_ATTEST_ENABLED=1` and `ROOM_ATTEST_ACTORS=dot` only for `vera3-mcp` through its compose environment. The default is off. Confirm the existing OAuth principal is exactly `dot`; do not create a new owner credential or broaden room access. Before a live attempt, read current task/qid/revision and inspect the owner's exact confirmation in the dot chat. Do not infer it from a code phrase. Check the destination and prior receipt. Monitor the single `delegated_attestation` event, answer authorship, `done` event and receipt; investigate any duplicate or mismatch before another attempt.

For `help-vc-7a37fd482d018b1a/qid2`, the reply `Привет!` was already sent at 10:21:06 UTC with receipt `Sentinel_3d38696ef9fc81918b7c81c63280117c`. Never send it again. The permitted literal word is `Привет`; the completion tool still requires exact equality, so this receipt must not be bound by claiming it carried `Привет`. Keep the task open until that mismatch has a separate reviewed resolution.

CI on PR #27 at `ef5e28d75cac772c3b766f5e21103af29693588b` passed 3,313 tests including PostgreSQL migration tests. Later completion guards require their own CI run before merge. Production preflight remains unverified: the available read-only SSH key was refused by SSH file-permission checks; no production configuration was changed to bypass that failure.
