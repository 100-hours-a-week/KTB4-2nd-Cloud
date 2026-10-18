#!/usr/bin/env python3
"""Short, read-only host/DB samples for a V1 staging trip-create run."""

import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time
from datetime import datetime, timezone


RUN_ID = re.compile(r'^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$')
PROCESS_NAMES = {'java', 'mysqld', 'proxysql', 'nginx', 'k6', 'docker-proxy'}
ROOT = Path('/run/yeodam-trip-diagnostics')


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def process_stats():
    result = {}
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / 'stat').read_text()
            name = raw.split('(', 1)[1].rsplit(')', 1)[0]
            if name not in PROCESS_NAMES:
                continue
            fields = raw.rsplit(')', 1)[1].split()
            # /proc/[pid]/stat fields 14, 15, 20, 24 after removing pid/comm.
            result[int(entry.name)] = (name, int(fields[11]) + int(fields[12]),
                                       int(fields[17]), int(fields[21]) * os.sysconf('SC_PAGE_SIZE'))
        except (OSError, ValueError, IndexError):
            continue
    return result


def cpu_sample(previous, current, elapsed, ticks_per_second):
    groups = {}
    for pid, (name, ticks, threads, rss) in current.items():
        group = groups.setdefault(name, {'cpuPctOfOneCore': 0.0, 'rssBytes': 0, 'threads': 0, 'processes': 0})
        if pid in previous and previous[pid][0] == name:
            group['cpuPctOfOneCore'] += max(0, ticks - previous[pid][1]) * 100 / ticks_per_second / elapsed
        group['rssBytes'] += rss
        group['threads'] += threads
        group['processes'] += 1
    for group in groups.values():
        group['cpuPctOfOneCore'] = round(group['cpuPctOfOneCore'], 2)
    return groups


def host_cpu(previous, current):
    if previous is None:
        return None
    total = sum(current) - sum(previous)
    idle = current[3] + current[4] - previous[3] - previous[4]
    return round((total - idle) * 100 / total, 2) if total > 0 else None


def mysql_query(args, sql, password=None):
    env = os.environ.copy()
    if password is not None:
        env['MYSQL_PWD'] = password
    result = subprocess.run(args, input=sql, text=True, capture_output=True,
                            env=env, timeout=4, check=False)
    if result.returncode:
        raise RuntimeError('read-only MySQL query failed')
    return result.stdout.strip()


def db_sample():
    source = mysql_query(['mysql', '--protocol=socket', '-uroot', '--batch', '--skip-column-names'],
                         "SHOW GLOBAL STATUS WHERE Variable_name IN "
                         "('Threads_running','Threads_connected','Questions','Com_commit','Com_insert',"
                         "'Innodb_row_lock_waits','Innodb_row_lock_time','Innodb_log_waits','Innodb_data_fsyncs');")
    password = Path('/etc/yeodam/v2-staging-proxysql/admin-password').read_text().strip()
    proxy_args = ['mysql', '--protocol=tcp', '-h127.0.0.1', '-P16032', '-uradmin',
                  '--batch', '--skip-column-names']
    pool = mysql_query(proxy_args,
                       'SELECT ConnUsed,ConnFree,ConnERR,Queries FROM stats_mysql_connection_pool '
                       'WHERE hostgroup=10;', password)
    digest = mysql_query(proxy_args,
                         'SELECT digest,count_star,sum_time,max_time FROM stats_mysql_query_digest '
                         'WHERE hostgroup=10 ORDER BY sum_time DESC LIMIT 20;', password)
    return {'sourceStatus': source, 'proxyPool': pool, 'proxyDigest': digest}


def collect_db(output_root, run_id, stop):
    output = output_root / f'{run_id}-source-db.jsonl'
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', buffering=1) as stream:
        while not stop.is_set():
            try:
                stream.write(json.dumps({'event': 'db', 'time': timestamp(), **db_sample()}) + '\n')
            except (OSError, RuntimeError, subprocess.TimeoutExpired):
                stream.write(json.dumps({'event': 'db_error', 'time': timestamp()}) + '\n')
            stop.wait(5)


def collect(role, run_id, duration, interval, output_root=ROOT):
    output_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = output_root / f'{role}.lock'
    with lock_path.open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        output = output_root / f'{run_id}-{role}.jsonl'
        fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        stop_db = threading.Event()
        db_thread = None
        if role == 'source':
            db_thread = threading.Thread(target=collect_db, args=(output_root, run_id, stop_db))
            db_thread.start()
        with os.fdopen(fd, 'w', buffering=1) as stream:
            stream.write(json.dumps({'event': 'started', 'time': timestamp(), 'role': role,
                                     'runId': run_id, 'intervalSeconds': interval,
                                     'cpuCount': os.cpu_count()}) + '\n')
            hz = os.sysconf('SC_CLK_TCK')
            previous_processes = {}
            previous_host = None
            previous_time = time.monotonic()
            end = previous_time + duration
            while time.monotonic() < end:
                now = time.monotonic()
                processes = process_stats()
                host = [int(value) for value in Path('/proc/stat').read_text().splitlines()[0].split()[1:9]]
                sample = {'event': 'cpu', 'time': timestamp(), 'hostCpuPct': host_cpu(previous_host, host),
                          'groups': cpu_sample(previous_processes, processes,
                                               max(0.001, now - previous_time), hz)}
                stream.write(json.dumps(sample) + '\n')
                previous_processes, previous_host, previous_time = processes, host, now
                time.sleep(max(0, interval - (time.monotonic() - now)))
            stream.write(json.dumps({'event': 'stopped', 'time': timestamp()}) + '\n')
        stop_db.set()
        if db_thread:
            db_thread.join(timeout=15)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--role', choices=('source', 'generator'), required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--duration-seconds', type=int, default=180)
    parser.add_argument('--interval-seconds', type=float, default=1.0)
    args = parser.parse_args()
    if not RUN_ID.fullmatch(args.run_id) or not 1 <= args.duration_seconds <= 240 or not 0.5 <= args.interval_seconds <= 5:
        parser.error('invalid run ID, duration, or interval')
    print(collect(args.role, args.run_id, args.duration_seconds, args.interval_seconds))


if __name__ == '__main__':
    main()
