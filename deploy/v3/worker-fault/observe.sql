SET SESSION MAX_EXECUTION_TIME=1500;
SET SESSION time_zone='+00:00';
START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY;
SELECT JSON_OBJECT('section','db_clock','tripId',__TRIP_ID__,'utc',UTC_TIMESTAMP(6));
SELECT JSON_OBJECT('section','execution_timing','tripId',trip_id,'executionId',execution_id,'status',status,'endedAt',ended_at,'expectedPhotos',total_attachment_count)
FROM yeodam.trip_creation_executions WHERE trip_id=__TRIP_ID__ LIMIT 2;
SELECT JSON_OBJECT('section','worker_task','tripId',trip_id,'taskId',task_id,'batchId',initial_batch_id,'kind',kind,'lane',lane,'status',status,'photos',total_photos,'claimHash',SHA2(claim_token,256),'leaseUntil',lease_until,'leaseSeconds',TIMESTAMPDIFF(MICROSECOND,UTC_TIMESTAMP(6),lease_until)/1000000,'currentItem',current_item_id,'currentAttempt',current_attempt_no,'attemptOwnerHash',SHA2(current_attempt_token,256),'deliveryNo',latest_delivery_no)
FROM yeodam.attachment_conversion_tasks WHERE trip_id=__TRIP_ID__ ORDER BY task_id LIMIT 3;
SELECT JSON_OBJECT('section','worker_item','tripId',b.trip_id,'batchId',b.upload_batch_id,'accepted',CAST(b.accepted_at IS NOT NULL AS UNSIGNED),'route',b.conversion_route,'itemId',i.upload_item_id,'attachmentId',i.trip_attachment_id,'attempt',i.conversion_attempts,'startedAt',i.conversion_started_at,'saved',CAST(i.derived_json IS NOT NULL AS UNSIGNED),'derivedHash',SHA2(i.derived_json,256),'cleanupHash',SHA2(i.cleanup_json,256),'cleanupEntries',CASE WHEN i.cleanup_json IS NULL THEN 0 WHEN JSON_VALID(i.cleanup_json) THEN JSON_LENGTH(i.cleanup_json) ELSE -1 END)
FROM yeodam.initial_attachment_upload_batches b JOIN yeodam.initial_attachment_upload_items i ON i.upload_batch_id=b.upload_batch_id WHERE b.trip_id=__TRIP_ID__ ORDER BY i.upload_item_id LIMIT 21;
SELECT JSON_OBJECT('section','worker_delivery','tripId',trip_id,'taskId',task_id,'deliveryNo',delivery_no,'status',status,'topic',topic,'partition',record_partition,'offset',record_offset,'traceparent',traceparent)
FROM yeodam.attachment_conversion_outbox WHERE trip_id=__TRIP_ID__ AND cdc_managed=TRUE ORDER BY created_at LIMIT 21;
SELECT JSON_OBJECT('section','final_attachment','tripId',trip_id,'attachmentId',trip_attachment_id,'fileId',file_id)
FROM yeodam.trip_attachments WHERE trip_id=__TRIP_ID__ AND deleted_at IS NULL ORDER BY trip_attachment_id LIMIT 21;
COMMIT;
