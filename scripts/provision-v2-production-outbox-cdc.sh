#!/usr/bin/env bash
set -Eeuo pipefail
set +x
umask 077
[[ $EUID -eq 0 ]] || { echo 'Run on the production MySQL host as root' >&2; exit 1; }
[[ ${CDC_KAFKA_HOST:-} =~ ^10\.20\.10\.[0-9]{1,3}$ ]] || exit 1
export CDC_KAFKA_HOST
[[ "${EXPECTED_INSTANCE_ID:-}" == i-* ]] || exit 1
token=$(curl --fail --silent --show-error --noproxy '*' --max-time 3 -X PUT -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' http://169.254.169.254/latest/api/token)
actual=$(curl --fail --silent --show-error --noproxy '*' --max-time 3 -H "X-aws-ec2-metadata-token: $token" http://169.254.169.254/latest/meta-data/instance-id)
[[ "$actual" = "$EXPECTED_INSTANCE_ID" ]] || { echo 'Wrong production MySQL host' >&2; exit 1; }
python3 - <<'PY'
import json, os, re, secrets, subprocess, tempfile
TABLES = ('trip_creation_outbox', 'additional_attachment_upload_outbox', 'story_generation_outbox')
def sql(query):
    result = subprocess.run(['docker','exec','-i','yeodam-v2-mysql','sh','-ec',
        'export MYSQL_PWD=$(cat /run/secrets/mysql-root-password); exec mysql -uroot --batch --skip-column-names'],
        input=query,text=True,capture_output=True)
    if result.returncode:
        raise SystemExit('MySQL CDC provisioning SQL failed; credentials were not printed')
    return result.stdout.strip()
assert sql('SELECT @@global.read_only,@@global.super_read_only;') == '0\t0', 'Production MySQL must be writable'
for table in TABLES:
    count=sql("SELECT COUNT(*) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA='yeodam' AND TABLE_NAME='%s' AND COLUMN_NAME IN ('outbox_id','trip_id','topic','event_source','cdc_managed','payload','request_id');" % table)
    assert count == '7', 'Confirmed Outbox columns missing: '+table
    count=sql("SELECT COUNT(*) FROM yeodam.%s WHERE cdc_managed=1 AND status<>'PUBLISHED';" % table)
    print('UNPUBLISHED_CDC_ROWS '+table+'='+count,flush=True)
parameter='/yeodam/v2/production/kafka/MYSQL_CDC_PASSWORD'
args=['aws','ssm','--region','ap-northeast-2']
r=subprocess.run(args+['get-parameter','--name',parameter,'--with-decryption'],capture_output=True,text=True)
if r.returncode == 0:
    password=json.loads(r.stdout)['Parameter']['Value']
elif 'ParameterNotFound' in r.stderr:
    password=secrets.token_hex(16)
    with tempfile.NamedTemporaryFile(mode='w',dir='/run') as request:
        json.dump({'Name':parameter,'Type':'SecureString','Value':password},request);request.flush()
        subprocess.run(args+['put-parameter','--cli-input-json','file://'+request.name],stdout=subprocess.DEVNULL,check=True)
else:
    raise SystemExit('CDC password read failed; credential unchanged')
assert re.fullmatch('[0-9a-f]{32}',password)
host=os.environ['CDC_KAFKA_HOST']
query="SET SESSION sql_log_bin=0; CREATE USER IF NOT EXISTS 'yeodam_outbox_cdc'@'%s' IDENTIFIED BY '%s' REQUIRE SSL; ALTER USER 'yeodam_outbox_cdc'@'%s' IDENTIFIED BY '%s' REQUIRE SSL; GRANT REPLICATION SLAVE,REPLICATION CLIENT ON *.* TO 'yeodam_outbox_cdc'@'%s';" % (host,password,host,password,host)
query+=''.join("GRANT SELECT ON yeodam.%s TO 'yeodam_outbox_cdc'@'%s';" % (t,host) for t in TABLES)
sql(query)
print('TLS-only CDC account ready; SELECT limited to three Outbox tables')
PY
