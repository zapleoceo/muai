-- 008: Drop Vera's own LLM-key store. All LLM calls go through the broker
-- (aib.zapleo.com); Vera holds no provider keys. 2026-06-29.
--
-- usage_log keeps recording the broker's mirrored usage, but its token_id
-- FK → tokens is meaningless now (broker_client never sets it). Drop the
-- column + its index, then drop the tokens table.

DROP INDEX IF EXISTS ix_usage_token_date;

ALTER TABLE usage_log DROP CONSTRAINT IF EXISTS usage_log_token_id_fkey;
ALTER TABLE usage_log DROP COLUMN IF EXISTS token_id;

DROP TABLE IF EXISTS tokens;
