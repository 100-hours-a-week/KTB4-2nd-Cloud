#!/usr/bin/env bash
set -Eeuo pipefail
[[ "$(id -u)" == 0 ]] || { echo 'Run as root on the validation MySQL host' >&2; exit 1; }
[[ -n "${MYSQL_METRICS_ADDRESS:-}" ]] || exit 1
image='quay.io/prometheus/mysqld-exporter:v0.20.0@sha256:abed8dac117b4ae5b70757f988e44795935b9a72c3b58d720c67ba688f8cb79e'
mysql_id=$(docker inspect --format '{{.Id}}' yeodam-v2-mysql)
mysql_started=$(docker inspect --format '{{.State.StartedAt}}' yeodam-v2-mysql)
install -d -m 0700 /etc/yeodam/mysql-validation-metrics
python3 - <<'PY'
import configparser, ipaddress, json, os, pathlib, secrets, subprocess, tempfile
base = pathlib.Path('/etc/yeodam/mysql-validation-metrics')
path = base / 'client.cnf'
address = ipaddress.ip_address(os.environ['MYSQL_METRICS_ADDRESS'])
assert address in ipaddress.ip_network('10.30.0.0/16')
container = json.loads(subprocess.check_output(['docker','inspect','yeodam-v2-mysql']))[0]
assert container['State']['Running']
gateway = container['NetworkSettings']['Networks']['bridge']['Gateway']
assert ipaddress.ip_address(gateway).is_private
password = secrets.token_hex(32)
if path.exists():
    config = configparser.ConfigParser(interpolation=None)
    config.read(path)
    password = config['client']['password']
assert len(password) == 64 and all(c in '0123456789abcdef' for c in password)
# Only this session suppresses account DDL in the binlog; app CDC is unchanged.
sql = f"""SET SESSION sql_log_bin=0;
CREATE USER IF NOT EXISTS 'yeodam_metrics'@'{gateway}' IDENTIFIED BY '{password}' WITH MAX_USER_CONNECTIONS 3;
ALTER USER 'yeodam_metrics'@'{gateway}' IDENTIFIED BY '{password}' WITH MAX_USER_CONNECTIONS 3;
SHOW GRANTS FOR 'yeodam_metrics'@'{gateway}';
"""
result = subprocess.run(['docker','exec','-i','yeodam-v2-mysql','sh','-c',
    'MYSQL_PWD=$(cat /run/secrets/mysql-root-password) exec mysql -uroot --batch --skip-column-names'],
    input=sql, text=True, capture_output=True)
if result.returncode:
    raise SystemExit('Metrics account provisioning failed; credential and SQL output withheld')
# This account deliberately has no SELECT on app tables and no global write privilege.
lines = [line for line in result.stdout.splitlines() if line.startswith('GRANT ')]
assert lines and all(line.startswith('GRANT USAGE ON *.* TO ') for line in lines)
with tempfile.NamedTemporaryFile(mode='w', dir=base, delete=False) as file:
    temporary = pathlib.Path(file.name)
    file.write('[client]\nuser=yeodam_metrics\npassword=' + password + '\nhost=127.0.0.1\nport=3306\n')
    os.fchmod(file.fileno(), 0o400)
    os.fchown(file.fileno(), 65534, 65534)
os.replace(temporary, path)
print('Metrics account ready: USAGE only; maximum 3 connections')
PY
docker pull "$image"
cat > /etc/systemd/system/yeodam-mysql-validation-metrics.service <<UNIT
[Unit]
Description=Yeodam validation MySQL status exporter
Requires=docker.service
After=docker.service network-online.target yeodam-mysql-validation.service
Wants=network-online.target
[Service]
Restart=on-failure
RestartSec=5
ExecStartPre=-/usr/bin/docker rm yeodam-mysql-validation-metrics
ExecStart=/usr/bin/docker run --name yeodam-mysql-validation-metrics --network host --user 65534:65534 --read-only --cap-drop ALL --security-opt no-new-privileges --memory 128m --cpus 0.25 --log-driver journald -v /etc/yeodam/mysql-validation-metrics/client.cnf:/run/secrets/mysql.cnf:ro $image --config.my-cnf=/run/secrets/mysql.cnf --mysqld.address=127.0.0.1:3306 --web.listen-address=$MYSQL_METRICS_ADDRESS:9104 --collect.global_status --collect.global_variables --no-collect.slave_status --no-collect.info_schema.innodb_cmp --no-collect.info_schema.innodb_cmpmem --no-collect.info_schema.query_response_time --exporter.query_timeout=5 --exporter.max_open_connections=2
ExecStop=/usr/bin/docker stop -t 10 yeodam-mysql-validation-metrics
TimeoutStopSec=20
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable yeodam-mysql-validation-metrics
systemctl restart yeodam-mysql-validation-metrics
for attempt in $(seq 1 18); do
  if curl --noproxy '*' -fsS --max-time 10 "http://$MYSQL_METRICS_ADDRESS:9104/metrics" | grep '^mysql_up 1$' >/dev/null; then
    [[ "$mysql_id" == "$(docker inspect --format '{{.Id}}' yeodam-v2-mysql)" ]]
    [[ "$mysql_started" == "$(docker inspect --format '{{.State.StartedAt}}' yeodam-v2-mysql)" ]]
    echo 'Validation MySQL container unchanged; mysql_up=1'
    exit 0
  fi
  sleep 3
done
echo 'Validation MySQL metrics did not become ready' >&2
exit 1
