ALTER TABLE additional_attachment_executions
    ADD COLUMN remote_terminal_confirmed_at DATETIME(6),
    ADD INDEX idx_additional_remote_timeout (trip_id, error_code, remote_terminal_confirmed_at);
