INSERT INTO yeodam.trips(trip_id,user_id,trip_name,start_date,end_date) VALUES
(92011,91001,'pending','2026-01-01','2026-01-02'),
(92012,91001,'known','2026-01-01','2026-01-02'),
(92013,91001,'complete','2026-01-01','2026-01-02');
INSERT INTO yeodam.trip_creation_executions(execution_id,trip_id,user_id,status,total_attachment_count,ended_at,process_payload,result_raw_payload) VALUES
('v40-failed-pending',92011,91001,'FAILED',1,'2026-01-01 00:00:00','{}',NULL),
('v40-failed-result',92012,91001,'FAILED',1,'2026-01-01 00:00:00','{}','{}'),
('v40-completed',92013,91001,'COMPLETED',1,'2026-01-01 00:00:00','{}',NULL);
INSERT INTO yeodam.additional_attachment_executions(execution_id,trip_id,user_id,status,total_attachment_count,ended_at,created_at,updated_at)
VALUES('v37-existing-actor',92001,91001,'COMPLETED',1,'2026-01-01 00:00:00','2026-01-01 00:00:00','2026-01-01 00:00:00');
