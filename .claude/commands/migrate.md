# /migrate

Apply a SQL migration to the production Vera 3 database.

## Facts

- Container `vera3-postgres`, user `vera`, database `vera`, host port
  `127.0.0.1:5433`.
- Migrations live in `vera3/infra/migrations/NNN_name.sql`.
- **There is no migration tracking table.** Nothing records what has been
  applied — order and idempotency are on you. Write every migration so
  re-running it is safe (`IF NOT EXISTS`, `ADD COLUMN IF NOT EXISTS`,
  `CREATE OR REPLACE`).
- `vera3-deploy` rsyncs the migration *files* to the server but never runs
  them. Applying is always a deliberate manual step.

## Steps

1. Show the user the exact SQL and get explicit confirmation.
2. Copy the file over and apply it:

```bash
scp vera3/infra/migrations/020_canonical_message_view.sql hetzner-root:/tmp/
ssh hetzner-root "docker exec -i vera3-postgres psql -U vera -d vera -v ON_ERROR_STOP=1 < /tmp/020_canonical_message_view.sql"
```

   For a one-liner:

```bash
ssh hetzner-root "docker exec vera3-postgres psql -U vera -d vera -v ON_ERROR_STOP=1 -c 'ALTER TABLE events ADD COLUMN IF NOT EXISTS foo text'"
```

3. Verify the object exists:

```bash
ssh hetzner-root "docker exec vera3-postgres psql -U vera -d vera -c '\d+ events'"
```

4. Commit the migration file to `master` in the same session — an applied
   migration that is not in git will diverge prod from the repo.

## Safety

- `ON_ERROR_STOP=1` always, so a multi-statement file aborts instead of
  half-applying.
- Confirm `DROP` / `TRUNCATE` / `DELETE` with the user before running. Never
  `DELETE FROM` without a `WHERE`.
- `events` is the large table — add columns as `DEFAULT NULL` (non-blocking)
  and create indexes `CONCURRENTLY` (which means: not inside a transaction
  block).
- Nightly backup is `/usr/local/bin/vera-backup.sh` (cron 03:30). Before
  anything destructive, take a fresh dump:

```bash
ssh hetzner-root "docker exec vera3-postgres pg_dump -U vera -d vera -Fc > /root/vera-pre-migration.dump"
```

## Known gap

Repo migrations jump `017 → 020` — `018` and `019` do not exist in the
tree. Before adding a new file, confirm the numbering with
`ls vera3/infra/migrations/` rather than assuming the next integer.
