ALTER TABLE decision_requests ADD COLUMN source_surface TEXT;
ALTER TABLE decision_requests ADD COLUMN source_session_key TEXT;

ALTER TABLE execution_attempts ADD COLUMN lease_token TEXT;
ALTER TABLE execution_attempts ADD COLUMN lease_expires_at TEXT;
ALTER TABLE execution_attempts ADD COLUMN consumer_id TEXT;
ALTER TABLE execution_attempts ADD COLUMN next_attempt_at TEXT;

CREATE INDEX IF NOT EXISTS idx_decision_stream_status
ON decision_requests(source_profile, source_session_id, name, status);

CREATE INDEX IF NOT EXISTS idx_execution_delivery_queue
ON execution_attempts(kind, status, next_attempt_at, created_at);
