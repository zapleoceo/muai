-- Migration 005: author_role / author_label
--
-- Backfills metadata.author_role + metadata.author_label and prepends an
-- "Author: <label> [<role>]" line to content_text for every telegram / gmail /
-- instagram event. New ingestor writes both fields natively (see userbot.py,
-- backfill.py, ingestor-gmail/poller.py, ingestor-instagram/__main__.py).
--
-- Why: chat_title in a personal chat = the OTHER party, but a sent message
-- in that chat is authored by the owner (Дима), not the counterparty.
-- Without an explicit author_role the LLM agent and SQL queries kept
-- defaulting to chat_title as the author label.
--
-- Idempotent: NOT LIKE 'Author:%' guard skips already-migrated rows.

BEGIN;

-- 1) metadata: author_role + author_label
UPDATE events
SET metadata = COALESCE(metadata, '{}'::jsonb)
  || jsonb_build_object(
       'author_role',
       CASE WHEN metadata->>'direction' = 'sent' THEN 'self'
            ELSE 'counterparty' END,
       'author_label',
       CASE WHEN metadata->>'direction' = 'sent' THEN 'Я'
            WHEN COALESCE(metadata->>'sender_username','') <> ''
              THEN '@' || (metadata->>'sender_username')
            WHEN source = 'gmail' AND COALESCE(metadata->>'from','') <> ''
              THEN metadata->>'from'
            ELSE COALESCE(metadata->>'chat_title','(unknown)') END
     )
WHERE source IN ('telegram','gmail','instagram')
  AND metadata->>'author_role' IS NULL;

-- 2) content_text: prepend Author: line
UPDATE events
SET content_text =
      'Author: ' || COALESCE(metadata->>'author_label','(unknown)')
                 || ' [' || COALESCE(metadata->>'author_role','counterparty') || ']'
                 || E'\n' || content_text
WHERE source IN ('telegram','gmail','instagram')
  AND content_text NOT LIKE 'Author:%';

COMMIT;
