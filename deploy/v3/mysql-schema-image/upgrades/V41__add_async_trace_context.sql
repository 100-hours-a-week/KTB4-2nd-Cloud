ALTER TABLE trip_creation_outbox
    ADD COLUMN traceparent VARCHAR(55) NULL,
    ADD COLUMN tracestate VARCHAR(512) NULL;

ALTER TABLE additional_attachment_upload_outbox
    ADD COLUMN traceparent VARCHAR(55) NULL,
    ADD COLUMN tracestate VARCHAR(512) NULL;

ALTER TABLE story_generation_outbox
    ADD COLUMN traceparent VARCHAR(55) NULL,
    ADD COLUMN tracestate VARCHAR(512) NULL;

ALTER TABLE attachment_conversion_outbox
    ADD COLUMN traceparent VARCHAR(55) NULL,
    ADD COLUMN tracestate VARCHAR(512) NULL;

ALTER TABLE trip_creation_executions
    ADD COLUMN result_traceparent VARCHAR(55) NULL,
    ADD COLUMN result_tracestate VARCHAR(512) NULL;
