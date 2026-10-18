#!/usr/bin/env bash
set -Eeuo pipefail
set +x
umask 077
[[ $EUID -eq 0 ]] || { echo 'Run on the V1 rehearsal source as root' >&2; exit 1; }
[[ ${CDC_KAFKA_HOST:-} =~ ^10\.30\.10\.[0-9]{1,3}$ ]] || { echo 'Expected private Kafka host IP' >&2; exit 1; }
[[ -s /var/lib/mysql/ca.pem ]] || { echo 'Source MySQL CA missing' >&2; exit 1; }
python3 - <<'PY'
import json, os, pathlib, re, secrets, subprocess, tempfile
parameter = '/yeodam/v2/staging/cdc-test/SOURCE_PASSWORD'
args = ['aws','ssm','--region','ap-northeast-2']
result = subprocess.run(args+['get-parameter','--name',parameter,'--with-decryption'],capture_output=True,text=True)
if result.returncode == 0:
    password = json.loads(result.stdout)['Parameter']['Value']
elif 'ParameterNotFound' in result.stderr:
    password = secrets.token_hex(16)
    with tempfile.NamedTemporaryFile(mode='w',dir='/run') as request:
        json.dump({'Name':parameter,'Type':'SecureString','Value':password},request)
        request.flush()
        subprocess.run(args+['put-parameter','--cli-input-json','file://'+request.name],stdout=subprocess.DEVNULL,check=True)
else:
    raise SystemExit('B1 source password read failed; credential unchanged')
assert re.fullmatch('[0-9a-f]{32}',password)
host = os.environ['CDC_KAFKA_HOST']
query = ("SET SESSION sql_log_bin=0; "
         "CREATE USER IF NOT EXISTS 'yeodam_cdc_test'@'%s' IDENTIFIED BY '%s' REQUIRE SSL; "
         "ALTER USER 'yeodam_cdc_test'@'%s' IDENTIFIED BY '%s' REQUIRE SSL; "
         "GRANT REPLICATION SLAVE, REPLICATION CLIENT ON *.* TO 'yeodam_cdc_test'@'%s'; " %
         (host,password,host,password,host))
for table in ('users','trips','trip_regions'):
    query += "GRANT SELECT ON yeodam_v1_source.%s TO 'yeodam_cdc_test'@'%s'; " % (table,host)
result = subprocess.run(['sudo','mysql','--protocol=socket','-uroot'],input=query,text=True,capture_output=True)
if result.returncode:
    raise SystemExit('B1 source account SQL failed; credentials were not printed')
PY
aws ssm put-parameter --region ap-northeast-2 \
  --name /yeodam/v2/staging/cdc-test/SOURCE_CA_PEM --type String \
  --value file:///var/lib/mysql/ca.pem --overwrite --query Version --output text >/dev/null
sudo mysql --protocol=socket -uroot -NBe \
  "SELECT user,host,ssl_type FROM mysql.user WHERE user='yeodam_cdc_test';"
echo 'B1 source CDC account ready; TLS required and SELECT limited to users/trips/trip_regions.'
