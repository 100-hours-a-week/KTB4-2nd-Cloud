-- Run on each approved source/target using an existing protected mysql client config.
-- mysql --batch --raw --skip-column-names < mysql-metadata.sql > metadata.jsonl
-- No business rows, credentials, DDL, writes, backup or replication control.
SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ;
START TRANSACTION READ ONLY;
SELECT JSON_OBJECT('kind','meta','schema','yeodam','engine',VERSION(),'serverUuid',@@server_uuid,
 'capturedAt',DATE_FORMAT(UTC_TIMESTAMP(),'%Y-%m-%dT%H:%i:%sZ'),'readOnly',@@global.read_only);
SELECT JSON_OBJECT('kind','history','rank',installed_rank,'version',version,'script',script,
 'checksum',checksum,'success',success) FROM yeodam.flyway_schema_history ORDER BY installed_rank;
SELECT JSON_OBJECT('kind','table','table',table_name,'estimatedRows',table_rows,
 'dataBytes',data_length,'indexBytes',index_length) FROM information_schema.tables
 WHERE table_schema='yeodam' AND table_type='BASE TABLE' ORDER BY table_name;
SELECT JSON_OBJECT('kind','column','table',table_name,'column',column_name,'position',ordinal_position,
 'type',column_type,'nullable',is_nullable,'collation',collation_name)
 FROM information_schema.columns WHERE table_schema='yeodam' ORDER BY table_name,ordinal_position;
SELECT JSON_OBJECT('kind','foreignKey','table',table_name,'constraint',constraint_name,'column',column_name,
 'position',ordinal_position,'parentTable',referenced_table_name,'parentColumn',referenced_column_name)
 FROM information_schema.key_column_usage WHERE table_schema='yeodam' AND referenced_table_name IS NOT NULL
 ORDER BY table_name,constraint_name,ordinal_position;
COMMIT;
