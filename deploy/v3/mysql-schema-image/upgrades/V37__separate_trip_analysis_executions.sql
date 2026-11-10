ALTER TABLE additional_attachment_executions
    ADD COLUMN processing_mode VARCHAR(8) NOT NULL DEFAULT 'LEGACY',
    ADD COLUMN accepted_actor_id BIGINT UNSIGNED,
    ADD COLUMN analysis_failure_count INT NOT NULL DEFAULT 0,
    ADD COLUMN quarantined_at DATETIME(6),
    ADD CONSTRAINT fk_additional_execution_actor
        FOREIGN KEY (accepted_actor_id) REFERENCES users (user_id),
    ADD CONSTRAINT ck_additional_execution_mode CHECK (processing_mode IN ('LEGACY', 'V3')),
    ADD CONSTRAINT ck_additional_analysis_failures CHECK (analysis_failure_count BETWEEN 0 AND 2);

UPDATE additional_attachment_executions SET accepted_actor_id = user_id;

CREATE TABLE trip_analysis_executions (
    execution_id VARCHAR(36) NOT NULL PRIMARY KEY,
    trip_id BIGINT UNSIGNED NOT NULL,
    owner_id BIGINT UNSIGNED NOT NULL,
    status VARCHAR(24) NOT NULL DEFAULT 'AI_REQUESTED',
    process_outbox_id BIGINT,
    process_published_at DATETIME(6),
    result_raw_payload LONGTEXT,
    result_ai_status VARCHAR(20),
    result_received_at DATETIME(6),
    result_conflicts LONGTEXT,
    result_attempts INT NOT NULL DEFAULT 0,
    result_claim_token VARCHAR(36),
    result_lease_until DATETIME(6),
    result_next_attempt_at DATETIME(6),
    result_failure_reason VARCHAR(255),
    requires_remote_termination_confirmation BOOLEAN NOT NULL DEFAULT FALSE,
    remote_terminal_confirmed_at DATETIME(6),
    ended_at DATETIME(6),
    error_code VARCHAR(100),
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    active_trip_id BIGINT UNSIGNED GENERATED ALWAYS AS
        (CASE WHEN ended_at IS NULL THEN trip_id ELSE NULL END) STORED,
    CONSTRAINT uk_trip_analysis_active UNIQUE (active_trip_id),
    CONSTRAINT fk_trip_analysis_trip FOREIGN KEY (trip_id) REFERENCES trips (trip_id),
    CONSTRAINT fk_trip_analysis_owner FOREIGN KEY (owner_id) REFERENCES users (user_id),
    CONSTRAINT fk_trip_analysis_input FOREIGN KEY (process_outbox_id)
        REFERENCES additional_attachment_upload_outbox (outbox_id),
    CONSTRAINT ck_trip_analysis_status CHECK (
        (ended_at IS NULL AND status IN ('AI_REQUESTED', 'RESULT_APPLYING', 'APPLY_BLOCKED'))
        OR (ended_at IS NOT NULL AND status IN ('COMPLETED', 'FAILED', 'CANCELED'))
    ),
    CONSTRAINT ck_trip_analysis_attempts CHECK (result_attempts >= 0),
    INDEX idx_trip_analysis_history (trip_id, created_at, execution_id),
    INDEX idx_trip_analysis_result (status, result_next_attempt_at, result_lease_until),
    INDEX idx_trip_analysis_remote (requires_remote_termination_confirmation, remote_terminal_confirmed_at)
) ENGINE = InnoDB DEFAULT CHARACTER SET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci;

CREATE TABLE trip_analysis_execution_uploads (
    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    execution_id VARCHAR(36) NOT NULL,
    addition_id VARCHAR(36) NOT NULL,
    CONSTRAINT uk_trip_analysis_upload UNIQUE (execution_id, addition_id),
    CONSTRAINT fk_trip_analysis_upload_execution FOREIGN KEY (execution_id)
        REFERENCES trip_analysis_executions (execution_id),
    CONSTRAINT fk_trip_analysis_upload_addition FOREIGN KEY (addition_id)
        REFERENCES additional_attachment_executions (execution_id),
    INDEX idx_trip_analysis_upload_addition (addition_id)
) ENGINE = InnoDB DEFAULT CHARACTER SET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci;

ALTER TABLE additional_attachment_upload_outbox
    MODIFY COLUMN upload_batch_id BIGINT NULL,
    ADD COLUMN analysis_execution_id VARCHAR(36),
    ADD COLUMN execution_batch_no INT,
    ADD COLUMN legacy_upload_batch_id BIGINT GENERATED ALWAYS AS
        (CASE WHEN analysis_execution_id IS NULL THEN upload_batch_id ELSE NULL END) STORED,
    ADD INDEX idx_additional_outbox_upload_batch (upload_batch_id),
    ADD CONSTRAINT fk_additional_outbox_analysis FOREIGN KEY (analysis_execution_id)
        REFERENCES trip_analysis_executions (execution_id),
    ADD CONSTRAINT uk_additional_analysis_message
        UNIQUE (analysis_execution_id, message_type, execution_batch_no),
    ADD CONSTRAINT uk_additional_legacy_message UNIQUE (legacy_upload_batch_id, message_type),
    ADD CONSTRAINT ck_additional_outbox_analysis CHECK (
        (analysis_execution_id IS NULL AND upload_batch_id IS NOT NULL)
        OR (analysis_execution_id IS NOT NULL AND execution_batch_no IS NOT NULL AND (
            (message_type = 'process' AND upload_batch_id IS NULL AND execution_batch_no = 0)
            OR (message_type = 'photos_ready' AND upload_batch_id IS NOT NULL AND execution_batch_no > 0)
        ))
    );

ALTER TABLE additional_attachment_upload_outbox DROP INDEX uk_additional_outbox_batch_type;

CREATE TABLE trip_photo_deletion_reservations (
    reservation_id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    trip_id BIGINT UNSIGNED NOT NULL,
    attachment_id BIGINT NOT NULL,
    actor_id BIGINT UNSIGNED NOT NULL,
    request_id VARCHAR(128) NOT NULL,
    related_addition_ids LONGTEXT NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'PENDING',
    requested_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    applied_at DATETIME(6),
    pending_attachment_id BIGINT GENERATED ALWAYS AS
        (CASE WHEN status = 'PENDING' THEN attachment_id ELSE NULL END) STORED,
    CONSTRAINT uk_trip_photo_pending_deletion UNIQUE (pending_attachment_id),
    CONSTRAINT fk_trip_photo_deletion_trip FOREIGN KEY (trip_id) REFERENCES trips (trip_id),
    CONSTRAINT fk_trip_photo_deletion_attachment FOREIGN KEY (attachment_id)
        REFERENCES trip_attachments (trip_attachment_id),
    CONSTRAINT fk_trip_photo_deletion_actor FOREIGN KEY (actor_id) REFERENCES users (user_id),
    CONSTRAINT ck_trip_photo_deletion_state CHECK (
        (status = 'PENDING' AND applied_at IS NULL) OR (status = 'APPLIED' AND applied_at IS NOT NULL)
    ),
    INDEX idx_trip_photo_deletion_pending (trip_id, status)
) ENGINE = InnoDB DEFAULT CHARACTER SET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci;
