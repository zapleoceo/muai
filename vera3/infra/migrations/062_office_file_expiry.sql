BEGIN;
SET LOCAL lock_timeout = '5s';
ALTER TABLE office_task_images ADD COLUMN IF NOT EXISTS expires_at timestamp;
CREATE INDEX IF NOT EXISTS ix_office_file_expiry
    ON office_task_images (expires_at) WHERE expires_at IS NOT NULL;
COMMIT;
