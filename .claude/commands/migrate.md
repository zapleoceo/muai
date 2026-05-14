# /migrate

Apply a SQL migration to the production database.

## Important constraints
- Alembic cannot run inside the container (no psycopg2, only asyncpg).
- Always apply via `psql` directly on the `tgbot-db-1` container.

## Steps
1. Show the SQL that will be applied (always confirm with user first).
2. Run:
```bash
ssh hetzner-root "docker compose -f /var/www/tgbot/docker-compose.yml exec -T db psql -U bot tgbot -c 'SQL_HERE'"
```
3. Verify the table/column exists after migration.

## Safety rules
- Never run DROP TABLE or TRUNCATE in production without explicit user confirmation.
- For large tables, add columns with `ALTER TABLE ... ADD COLUMN ... DEFAULT NULL` (non-blocking).
- Always test the SQL logic locally first if possible.
