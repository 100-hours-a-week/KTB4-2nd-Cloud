SET SESSION time_zone = '+00:00';
SET SESSION TRANSACTION READ ONLY;
START TRANSACTION WITH CONSISTENT SNAPSHOT;
SELECT /*+ MAX_EXECUTION_TIME(750) */ 'trip_creation_outbox', COUNT(*), COALESCE(GREATEST(0,TIMESTAMPDIFF(MICROSECOND,MIN(created_at),UTC_TIMESTAMP(6)))/1000000,0) FROM (SELECT created_at FROM yeodam.trip_creation_outbox WHERE cdc_managed=TRUE AND status IN ('PENDING','PUBLISHING') ORDER BY created_at LIMIT 10001) q;
SELECT /*+ MAX_EXECUTION_TIME(750) */ 'additional_attachment_upload_outbox', COUNT(*), COALESCE(GREATEST(0,TIMESTAMPDIFF(MICROSECOND,MIN(created_at),UTC_TIMESTAMP(6)))/1000000,0) FROM (SELECT created_at FROM yeodam.additional_attachment_upload_outbox WHERE cdc_managed=TRUE AND status IN ('PENDING','PUBLISHING') ORDER BY created_at LIMIT 10001) q;
SELECT /*+ MAX_EXECUTION_TIME(750) */ 'story_generation_outbox', COUNT(*), COALESCE(GREATEST(0,TIMESTAMPDIFF(MICROSECOND,MIN(created_at),UTC_TIMESTAMP(6)))/1000000,0) FROM (SELECT created_at FROM yeodam.story_generation_outbox WHERE cdc_managed=TRUE AND status IN ('PENDING','PUBLISHING') ORDER BY created_at LIMIT 10001) q;
SELECT /*+ MAX_EXECUTION_TIME(750) */ 'attachment_conversion_outbox', COUNT(*), COALESCE(GREATEST(0,TIMESTAMPDIFF(MICROSECOND,MIN(created_at),UTC_TIMESTAMP(6)))/1000000,0) FROM (SELECT created_at FROM yeodam.attachment_conversion_outbox WHERE cdc_managed=TRUE AND status IN ('PENDING','PUBLISHING') ORDER BY created_at LIMIT 10001) q;
-- Classify the SAME oldest 10001 unpublished conversion rows. Never hide orphan/unknown tasks.
-- When the raw count is capped all classification counts are lower bounds, not complete totals.
WITH pending AS (
  SELECT task_id, created_at FROM yeodam.attachment_conversion_outbox
  WHERE cdc_managed=TRUE AND status IN ('PENDING','PUBLISHING')
  ORDER BY created_at LIMIT 10001
), classified AS (
  SELECT p.created_at, CASE
    WHEN t.status IN ('WAITING','RUNNING') THEN 'active'
    WHEN t.status='SUCCEEDED' THEN 'succeeded'
    WHEN t.status='CANCELED' THEN 'canceled'
    WHEN t.status='FAILED' THEN 'failed'
    ELSE 'unknown' END AS category
  FROM pending p LEFT JOIN yeodam.attachment_conversion_tasks t ON t.task_id=p.task_id
)
SELECT /*+ MAX_EXECUTION_TIME(750) */ CONCAT('conversion_',b.category), COUNT(c.created_at),
  COALESCE(GREATEST(0,TIMESTAMPDIFF(MICROSECOND,MIN(c.created_at),UTC_TIMESTAMP(6)))/1000000,0)
FROM (SELECT 'active' category UNION ALL SELECT 'succeeded' UNION ALL SELECT 'canceled'
      UNION ALL SELECT 'failed' UNION ALL SELECT 'unknown') b
LEFT JOIN classified c ON c.category=b.category
GROUP BY b.category ORDER BY b.category;
COMMIT;
