ALTER TABLE trip_creation_executions
    ADD COLUMN requires_remote_termination_confirmation BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN remote_terminal_confirmed_at DATETIME(6) NULL;

UPDATE trip_creation_executions
SET requires_remote_termination_confirmation = TRUE
WHERE ended_at IS NOT NULL
  AND status IN ('FAILED', 'CANCELED')
  AND process_payload IS NOT NULL
  AND result_raw_payload IS NULL;
