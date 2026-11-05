BEGIN;

SET LOCAL lock_timeout = '5s';

ALTER TABLE room_task_origin
    ADD COLUMN IF NOT EXISTS pending_title varchar(200),
    ADD COLUMN IF NOT EXISTS pending_body varchar(8000),
    ADD COLUMN IF NOT EXISTS creator_project varchar(64);

CREATE TABLE IF NOT EXISTS office_task_images (
    room varchar(64) NOT NULL,
    task_id varchar(128) NOT NULL,
    attachment_index bigint NOT NULL,
    sha256 varchar(64) NOT NULL,
    mime varchar(32) NOT NULL,
    content bytea NOT NULL,
    created_at timestamp NOT NULL DEFAULT now(),
    PRIMARY KEY (room, task_id, attachment_index),
    FOREIGN KEY (room, task_id) REFERENCES room_task_origin(room, task_id) ON DELETE CASCADE
);

COMMIT;
