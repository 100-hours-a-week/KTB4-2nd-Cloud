SET SESSION TRANSACTION READ ONLY;
START TRANSACTION WITH CONSISTENT SNAPSHOT;
SELECT /*+ MAX_EXECUTION_TIME(750) */ JSON_OBJECT(
 'waiting',(SELECT COUNT(*) FROM yeodam.attachment_conversion_tasks WHERE status='WAITING'),
 'running',(SELECT COUNT(*) FROM yeodam.attachment_conversion_tasks WHERE status='RUNNING'),
 'unknown',(SELECT COUNT(*) FROM yeodam.attachment_conversion_tasks WHERE status IS NULL OR status NOT IN ('WAITING','RUNNING','SUCCEEDED','CANCELED','FAILED')) + (SELECT COUNT(*) FROM yeodam.attachment_conversion_outbox o LEFT JOIN yeodam.attachment_conversion_tasks t ON t.task_id=o.task_id WHERE o.cdc_managed=TRUE AND o.status IN ('PENDING','PUBLISHING') AND t.task_id IS NULL),
 'unpublishedActive',(SELECT COUNT(*) FROM yeodam.attachment_conversion_outbox o JOIN yeodam.attachment_conversion_tasks t ON t.task_id=o.task_id WHERE o.cdc_managed=TRUE AND o.status IN ('PENDING','PUBLISHING') AND t.status IN ('WAITING','RUNNING'))
);
COMMIT;
