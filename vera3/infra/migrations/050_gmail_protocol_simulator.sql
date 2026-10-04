-- Durable, synthetic-only Gmail protocol receipts. No network adapter or grants.
BEGIN;
CREATE TABLE IF NOT EXISTS brain_gmail_protocol_history (
    google_sub text PRIMARY KEY REFERENCES brain_gmail_pilot_accounts(google_sub),
    start_history_id text NOT NULL,
    generation bigint NOT NULL DEFAULT 1,
    expected_token text NOT NULL DEFAULT '',
    final_history_id text,
    state text NOT NULL DEFAULT 'paging',
    CHECK (state IN ('paging','ready','complete','expired'))
);
CREATE TABLE IF NOT EXISTS brain_gmail_protocol_history_pages (
    google_sub text NOT NULL REFERENCES brain_gmail_pilot_accounts(google_sub),
    start_history_id text NOT NULL,
    generation bigint NOT NULL,
    requested_token text NOT NULL,
    next_token text,
    response_history_id text NOT NULL,
    response_hash text NOT NULL,
    manifest jsonb NOT NULL,
    captured_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (google_sub,start_history_id,generation,requested_token)
);
CREATE TABLE IF NOT EXISTS brain_gmail_protocol_obligations (
    google_sub text NOT NULL REFERENCES brain_gmail_pilot_accounts(google_sub),
    start_history_id text NOT NULL,
    generation bigint NOT NULL,
    event_key text NOT NULL,
    history_id text NOT NULL,
    message_id text NOT NULL,
    kind text NOT NULL,
    label_ids jsonb NOT NULL DEFAULT '[]',
    PRIMARY KEY (google_sub,start_history_id,generation,event_key)
);
CREATE TABLE IF NOT EXISTS brain_gmail_protocol_full_sync (
    google_sub text PRIMARY KEY REFERENCES brain_gmail_pilot_accounts(google_sub),
    generation bigint NOT NULL DEFAULT 1,
    scope jsonb NOT NULL,
    window_start timestamptz NOT NULL,
    window_end timestamptz NOT NULL,
    started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    completed_at timestamptz,
    anchor_history_id text,
    expected_token text NOT NULL DEFAULT '',
    state text NOT NULL DEFAULT 'paging',
    CHECK (window_start < window_end),
    CHECK (state IN ('paging','ready','bridging','complete','ambiguous'))
);
CREATE TABLE IF NOT EXISTS brain_gmail_protocol_full_cycles (
    google_sub text NOT NULL REFERENCES brain_gmail_pilot_accounts(google_sub),
    generation bigint NOT NULL,
    started_at timestamptz NOT NULL,
    completed_at timestamptz,
    PRIMARY KEY (google_sub,generation)
);
CREATE TABLE IF NOT EXISTS brain_gmail_protocol_full_pages (
    google_sub text NOT NULL REFERENCES brain_gmail_pilot_accounts(google_sub),
    generation bigint NOT NULL,
    requested_token text NOT NULL,
    next_token text,
    response_hash text NOT NULL,
    manifest jsonb NOT NULL,
    captured_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (google_sub,generation,requested_token)
);
CREATE TABLE IF NOT EXISTS brain_gmail_protocol_full_items (
    google_sub text NOT NULL REFERENCES brain_gmail_pilot_accounts(google_sub),
    generation bigint NOT NULL,
    message_id text NOT NULL,
    payload jsonb NOT NULL,
    PRIMARY KEY (google_sub,generation,message_id)
);
INSERT INTO schema_migrations(version,note)
VALUES ('050_gmail_protocol_simulator','durable offline Gmail protocol receipts')
ON CONFLICT(version) DO NOTHING;
COMMIT;
