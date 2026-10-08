-- Room-only OAuth state. All opaque codes/tokens are stored as SHA-256 digests.
-- Client metadata (including DCR client secret) is encrypted by ROOM_OAUTH_KEY.
BEGIN;
CREATE TABLE IF NOT EXISTS room_oauth_clients (
    client_id varchar(512) PRIMARY KEY,
    encrypted_info text NOT NULL
);

CREATE TABLE IF NOT EXISTS room_oauth_grants (
    token_hash varchar(64) PRIMARY KEY,
    kind varchar(16) NOT NULL CHECK (kind IN ('pending', 'code', 'access', 'refresh')),
    client_id varchar(512) NOT NULL,
    actor varchar(64),
    data_json text NOT NULL,
    expires_at timestamptz NOT NULL,
    family_id varchar(64)
);

CREATE INDEX IF NOT EXISTS ix_room_oauth_grants_client ON room_oauth_grants (client_id);
CREATE INDEX IF NOT EXISTS ix_room_oauth_grants_family ON room_oauth_grants (family_id);
CREATE INDEX IF NOT EXISTS ix_room_oauth_grants_expires ON room_oauth_grants (expires_at);

INSERT INTO schema_migrations (version, note)
VALUES ('048_room_oauth', 'room-only OAuth clients and hashed grants')
ON CONFLICT (version) DO NOTHING;

COMMIT;
