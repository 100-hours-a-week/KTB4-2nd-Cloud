-- Cloud-only probe, executed after Connector RUNNING. Values supplied by the runner.
-- Terminal parents prevent application schedulers from treating this fixture as real work.
START TRANSACTION;
INSERT INTO users(email,nickname) VALUES (@probe_email,'CdcProbe');
SET @probe_user=LAST_INSERT_ID();
INSERT INTO trips(user_id,trip_name,start_date,end_date,processing_status)
VALUES (@probe_user,'CdcProbe',CURRENT_DATE,CURRENT_DATE,'COMPLETED');
SET @probe_trip=LAST_INSERT_ID();
INSERT INTO trip_creation_executions(execution_id,trip_id,user_id,status,total_attachment_count,input_closed,ended_at)
VALUES (@probe_execution,@probe_trip,@probe_user,'CANCELED',1,TRUE,NOW(6));
INSERT INTO story_generation_jobs(execution_id,trip_id,user_id,mood,status)
VALUES (@probe_story_execution,@probe_trip,@probe_user,'CALM','CANCELED');
SET @probe_story=LAST_INSERT_ID();
INSERT INTO additional_attachment_upload_batches(upload_id,addition_id,trip_id,user_id,batch_no,total_attachment_count,is_last_batch,status)
VALUES (@probe_upload,@probe_execution,@probe_trip,@probe_user,1,1,TRUE,'FAILED');
SET @probe_batch=LAST_INSERT_ID();
INSERT INTO trip_creation_outbox(outbox_id,execution_id,message_type,batch_no,trip_id,topic,payload,request_id)
VALUES (@probe_positive,@probe_execution,'photos_ready',1,@probe_trip,@probe_topic,@probe_trip_payload,@probe_run);
INSERT INTO story_generation_outbox(generation_id,payload,request_id,trip_id,topic,cdc_managed)
VALUES (@probe_story,@probe_story_payload,@probe_run,@probe_trip,@probe_topic,TRUE);
SET @probe_story_outbox=LAST_INSERT_ID();
INSERT INTO additional_attachment_upload_outbox(upload_batch_id,message_type,payload,request_id,trip_id,topic,cdc_managed)
VALUES (@probe_batch,'photos_ready',@probe_update_payload,@probe_run,@probe_trip,@probe_topic,TRUE);
SET @probe_update_outbox=LAST_INSERT_ID();
INSERT INTO trip_creation_outbox(outbox_id,execution_id,message_type,batch_no,trip_id,topic,payload,request_id,cdc_managed)
VALUES (@probe_negative,@probe_execution,'photos_ready',2,@probe_trip,@probe_topic,@probe_negative_payload,@probe_run,FALSE);
COMMIT;
-- UPDATEs must never emit a second request, including a FALSE -> TRUE transition.
UPDATE trip_creation_outbox SET status='PUBLISHED' WHERE outbox_id=@probe_positive;
UPDATE story_generation_outbox SET status='PUBLISHED' WHERE outbox_id=@probe_story_outbox;
UPDATE additional_attachment_upload_outbox SET status='PUBLISHED' WHERE outbox_id=@probe_update_outbox;
UPDATE trip_creation_outbox SET cdc_managed=TRUE WHERE outbox_id=@probe_negative;
START TRANSACTION;
INSERT INTO trip_creation_outbox(outbox_id,execution_id,message_type,batch_no,trip_id,topic,payload,request_id)
VALUES (@probe_rollback,@probe_execution,'photos_ready',3,@probe_trip,@probe_topic,@probe_rollback_payload,@probe_run);
ROLLBACK;
SELECT JSON_OBJECT('user',@probe_user,'trip',@probe_trip,'story',@probe_story,'batch',@probe_batch,
 'story_outbox',@probe_story_outbox,'update_outbox',@probe_update_outbox);
