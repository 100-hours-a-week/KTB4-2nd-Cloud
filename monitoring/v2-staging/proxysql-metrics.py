#!/usr/bin/env python3
"""Root-only, read-only ProxySQL sampler for the existing Alloy textfile path."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

DIRECTORY = Path('/var/lib/yeodam/mysql-metrics')

def query(sql, password):
    env = dict(os.environ, MYSQL_PWD=password)
    result = subprocess.run(['mysql', '--protocol=tcp', '-h127.0.0.1', '-P16032', '-uradmin',
                             '--connect-timeout=2', '--batch', '--skip-column-names', '-e', sql],
                            env=env, capture_output=True, text=True, timeout=4)
    if result.returncode: raise RuntimeError('ProxySQL admin query failed')
    return [row.split('\t') for row in result.stdout.splitlines()]

def number(raw):
    value = int(raw)
    if value < 0: raise ValueError('Negative counter')
    return value

def sample(password):
    lines = []
    rows = query('SELECT hostgroup,srv_host,srv_port,status,ConnUsed,ConnFree,ConnERR,Queries FROM stats_mysql_connection_pool;', password)
    for hg, host, port, status, used, free, errors, queries in rows:
        if hg not in ('10', '20'): continue
        if any(c not in '0123456789.' for c in host): raise ValueError('Unexpected backend address')
        labels = f'hostgroup="{hg}",server="{host}:{number(port)}"'
        for name, value in [('connections_used', used), ('connections_free', free), ('connection_errors_total', errors), ('queries_total', queries)]:
            lines.append(f'yeodam_proxysql_{name}{{{labels}}} {number(value)}')
        lines.append(f'yeodam_proxysql_backend_online{{{labels}}} {int(status == "ONLINE")}')
    groups = query('SELECT DISTINCT default_hostgroup FROM runtime_mysql_users WHERE username="yeodam_app";', password)
    if len(groups) != 1 or groups[0][0] not in ('10', '20'): raise ValueError('Unexpected default hostgroup')
    lines.append(f'yeodam_proxysql_default_hostgroup {groups[0][0]}')
    globals = dict(query("SELECT Variable_Name,Variable_Value FROM stats_mysql_global WHERE Variable_Name IN ('Active_Transactions','Server_Connections_delayed','max_connect_timeouts','generated_error_packets','ConnPool_get_conn_failure');", password))
    for key, metric in [('Active_Transactions','active_transactions'), ('Server_Connections_delayed','connection_delays_total'),
                        ('max_connect_timeouts','connect_timeouts_total'), ('generated_error_packets','generated_errors_total'),
                        ('ConnPool_get_conn_failure','pool_connection_misses_total')]:
        if key in globals: lines.append(f'yeodam_proxysql_{metric} {number(globals[key])}')
    # This is a snapshot of active sessions without an assigned backend,
    # not the HTTP queue depth nor the total duration of all waiting queries.
    pending = query('SELECT COUNT(*),COALESCE(MAX(time_ms),0) FROM stats_mysql_processlist WHERE user="yeodam_app" AND command!="Sleep" AND (srv_host IS NULL OR srv_host="");', password)
    lines.append(f'yeodam_proxysql_unassigned_active_sessions {number(pending[0][0])}')
    lines.append(f'yeodam_proxysql_unassigned_state_max_seconds {number(pending[0][1]) / 1000}')
    backend = subprocess.check_output(['docker','ps','--filter','label=com.docker.compose.project=yeodam-app',
                                       '--filter','label=com.docker.compose.service=backend','--quiet'], text=True, timeout=3).strip()
    if not backend or '\n' in backend: raise RuntimeError('Expected one Backend container')
    url = subprocess.check_output(['docker','exec',backend,'sh','-c','printf %s "$MYSQL_URL"'],text=True,timeout=3)
    route = 1 if url == 'jdbc:mysql://proxysql:6033/yeodam_v1_source' else 0 if url == 'jdbc:mysql://host.docker.internal:3306/yeodam_v1_source' else -1
    lines.append(f'yeodam_backend_uses_proxysql {route}')
    return lines

def main():
    if os.geteuid() != 0: raise RuntimeError('Run as root on the V1 staging App')
    DIRECTORY.mkdir(parents=True, exist_ok=True)
    started = time.time()
    try:
        password = Path('/etc/yeodam/v2-staging-proxysql/admin-password').read_text().strip()
        lines = sample(password)
        success = 1
    except (RuntimeError, ValueError, OSError, subprocess.SubprocessError):
        lines = []
        success = 0
        print('ProxySQL metric collection failed; inspect the local service and admin access', file=sys.stderr)
    lines += [f'yeodam_proxysql_probe_success {success}', f'yeodam_proxysql_sample_timestamp_seconds {started}',
              f'yeodam_proxysql_scrape_duration_seconds {time.time() - started}']
    descriptor, temporary = tempfile.mkstemp(prefix='.proxysql-', dir=DIRECTORY)
    try:
        with os.fdopen(descriptor, 'w') as file: file.write('\n'.join(lines) + '\n')
        os.chmod(temporary,0o644)
        os.replace(temporary,DIRECTORY/'proxysql.prom')
    finally:
        if os.path.exists(temporary): os.unlink(temporary)
    return 0 if success else 1

if __name__ == '__main__':
    raise SystemExit(main())
