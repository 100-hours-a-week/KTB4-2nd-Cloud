-- Disposable CI database only. Never execute against staging/production.
CREATE USER 'ci_cdc'@'%' IDENTIFIED BY 'ci-cdc-only';
GRANT REPLICATION SLAVE, REPLICATION CLIENT ON *.* TO 'ci_cdc'@'%';
GRANT SELECT ON yeodam_v2_ci.trip_creation_outbox TO 'ci_cdc'@'%';
GRANT SELECT ON yeodam_v2_ci.additional_attachment_upload_outbox TO 'ci_cdc'@'%';
GRANT SELECT ON yeodam_v2_ci.story_generation_outbox TO 'ci_cdc'@'%';
INSERT INTO users (user_id, email, nickname)
VALUES (900001, 'cloud-v2-ci@example.invalid', 'V2검증');
INSERT INTO user_stats (user_id) VALUES (900001);
INSERT INTO consents (user_id, is_agreed, agreed_at)
VALUES (900001, TRUE, CURRENT_TIMESTAMP(6));
