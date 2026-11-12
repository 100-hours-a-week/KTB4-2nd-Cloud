SET SESSION time_zone = '+00:00';
SET SESSION TRANSACTION READ ONLY;
START TRANSACTION WITH CONSISTENT SNAPSHOT;
SELECT /*+ MAX_EXECUTION_TIME(750) */ 'trip_creation_outbox', COUNT(*), COALESCE(GREATEST(0,TIMESTAMPDIFF(MICROSECOND,MIN(created_at),UTC_TIMESTAMP(6)))/1000000,0) FROM (SELECT created_at FROM yeodam.trip_creation_outbox WHERE cdc_managed=TRUE AND status IN ('PENDING','PUBLISHING') ORDER BY created_at LIMIT 10001) q;
SELECT /*+ MAX_EXECUTION_TIME(750) */ 'additional_attachment_upload_outbox', COUNT(*), COALESCE(GREATEST(0,TIMESTAMPDIFF(MICROSECOND,MIN(created_at),UTC_TIMESTAMP(6)))/1000000,0) FROM (SELECT created_at FROM yeodam.additional_attachment_upload_outbox WHERE cdc_managed=TRUE AND status IN ('PENDING','PUBLISHING') ORDER BY created_at LIMIT 10001) q;
SELECT /*+ MAX_EXECUTION_TIME(750) */ 'story_generation_outbox', COUNT(*), COALESCE(GREATEST(0,TIMESTAMPDIFF(MICROSECOND,MIN(created_at),UTC_TIMESTAMP(6)))/1000000,0) FROM (SELECT created_at FROM yeodam.story_generation_outbox WHERE cdc_managed=TRUE AND status IN ('PENDING','PUBLISHING') ORDER BY created_at LIMIT 10001) q;
SELECT /*+ MAX_EXECUTION_TIME(750) */ 'attachment_conversion_outbox', COUNT(*), COALESCE(GREATEST(0,TIMESTAMPDIFF(MICROSECOND,MIN(created_at),UTC_TIMESTAMP(6)))/1000000,0) FROM (SELECT created_at FROM yeodam.attachment_conversion_outbox WHERE cdc_managed=TRUE AND status IN ('PENDING','PUBLISHING') ORDER BY created_at LIMIT 10001) q;
COMMIT;
