PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS decision_requests (
    decision_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    title TEXT NOT NULL,
    source_profile TEXT NOT NULL,
    source_session_id TEXT NOT NULL,
    source_task_id TEXT NOT NULL,
    plugin_version TEXT NOT NULL,
    priority TEXT NOT NULL,
    status TEXT NOT NULL,
    decision_type TEXT NOT NULL,
    dedupe_hash TEXT NOT NULL,
    auto_resume INTEGER NOT NULL DEFAULT 1,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_decision_session_status
ON decision_requests(source_profile, source_session_id, status);
CREATE INDEX IF NOT EXISTS idx_decision_dedupe
ON decision_requests(source_profile, source_session_id, dedupe_hash, created_at);

CREATE TABLE IF NOT EXISTS decision_request_dedupes (
    source_profile TEXT NOT NULL,
    source_session_id TEXT NOT NULL,
    dedupe_hash TEXT NOT NULL,
    decision_id TEXT NOT NULL REFERENCES decision_requests(decision_id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    PRIMARY KEY(source_profile, source_session_id, dedupe_hash, created_at)
);
CREATE INDEX IF NOT EXISTS idx_request_dedupe_lookup
ON decision_request_dedupes(source_profile, source_session_id, dedupe_hash, created_at);

CREATE TABLE IF NOT EXISTS decision_cards (
    card_id TEXT PRIMARY KEY,
    decision_id TEXT NOT NULL REFERENCES decision_requests(decision_id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    details TEXT NOT NULL,
    source_profile TEXT NOT NULL,
    source_session_id TEXT NOT NULL,
    recommendation_option_id TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    execution_kind TEXT,
    execution_payload_json TEXT,
    base_revision TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL,
    priority TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    UNIQUE(decision_id, position)
);

CREATE TABLE IF NOT EXISTS decision_options (
    option_id TEXT NOT NULL,
    card_id TEXT NOT NULL REFERENCES decision_cards(card_id) ON DELETE CASCADE,
    label TEXT NOT NULL,
    details TEXT NOT NULL,
    reason TEXT NOT NULL,
    is_recommended INTEGER NOT NULL,
    position INTEGER NOT NULL,
    PRIMARY KEY(card_id, option_id)
);

CREATE TABLE IF NOT EXISTS card_responses (
    response_id TEXT PRIMARY KEY,
    card_id TEXT NOT NULL UNIQUE REFERENCES decision_cards(card_id) ON DELETE CASCADE,
    card_version INTEGER NOT NULL,
    outcome TEXT NOT NULL,
    selected_option_id TEXT,
    note TEXT NOT NULL,
    telegram_user_id INTEGER NOT NULL,
    decided_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS submission_manifests (
    manifest_id TEXT PRIMARY KEY,
    decision_id TEXT NOT NULL REFERENCES decision_requests(decision_id),
    submission_version INTEGER NOT NULL,
    sha256 TEXT NOT NULL UNIQUE,
    manifest_json TEXT NOT NULL,
    manifest_path TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(decision_id, submission_version)
);

CREATE TABLE IF NOT EXISTS execution_attempts (
    execution_id TEXT PRIMARY KEY,
    manifest_id TEXT NOT NULL REFERENCES submission_manifests(manifest_id),
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    run_id TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    result_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notification_outbox (
    notification_id TEXT PRIMARY KEY,
    decision_id TEXT REFERENCES decision_requests(decision_id),
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL,
    last_error TEXT,
    created_at TEXT NOT NULL,
    sent_at TEXT
);

CREATE TABLE IF NOT EXISTS audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id TEXT,
    actor TEXT NOT NULL,
    event_type TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS auth_nonces (
    nonce_hash TEXT PRIMARY KEY,
    telegram_user_id INTEGER NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);
