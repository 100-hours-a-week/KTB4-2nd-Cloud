#!/usr/bin/env python3
"""Optional #428 process/DB sampling. No SQL text, environment or process command arguments."""
import os
from pathlib import Path
import subprocess
import time

NAMES = {'nginx', 'java', 'mysqld', 'alloy', 'k6'}


def parse_stat(text):
    fields = text.rsplit(')', 1)[1].split()
    return int(fields[11]) + int(fields[12]), int(fields[19])


def process_percent(previous, current, seconds, hz):
    if not previous or previous[1] != current[1] or seconds <= 0:
        return None
    ticks = current[0] - previous[0]
    return None if ticks < 0 else round(100 * ticks / hz / seconds, 3)


class Sampler:
    def __init__(self):
        self.previous = {}
        self.at = None
        self.calls = 0
        self.hz = os.sysconf('SC_CLK_TCK')

    def sample(self):
        at = time.monotonic()
        seconds = at - self.at if self.at else 0
        rows, current = [], {}
        for directory in Path('/proc').iterdir():
            if not directory.name.isdigit():
                continue
            try:
                name = (directory/'comm').read_text().strip()
                if name not in NAMES:
                    continue
                stat = parse_stat((directory/'stat').read_text())
                current[directory.name] = stat
                limits = next(x.split()[-3:] for x in (directory/'limits').read_text().splitlines() if x.startswith('Max open files'))
                io = dict(x.split(':', 1) for x in (directory/'io').read_text().splitlines())
                rows.append({'pid': int(directory.name), 'name': name, 'cpuPercentOneCore': process_percent(
                    self.previous.get(directory.name), stat, seconds, self.hz),
                    'openFds': len(list((directory/'fd').iterdir())), 'fdSoftLimit': limits[0],
                    'readBytes': int(io['read_bytes']), 'writeBytes': int(io['write_bytes'])})
            except (FileNotFoundError, ProcessLookupError, PermissionError):
                continue
        self.previous, self.at = current, at
        result = {'processes': rows, 'processCpuUnit': '100%=one logical CPU; first/reused PID sample null'}
        if self.calls % 5 == 0:
            # Existing counters only. No global resets, logging/profile settings, locks or SQL text.
            try:
                r = subprocess.run(['mysql', '--protocol=socket', '--user=root', '--database=yeodam_v1_source',
                    '--batch', '--skip-column-names', '--execute', "SHOW GLOBAL STATUS WHERE Variable_name IN "
                    "('Threads_running','Threads_connected','Questions','Connections','Innodb_row_lock_current_waits',"
                    "'Innodb_row_lock_waits','Innodb_row_lock_time','Innodb_data_pending_reads','Innodb_data_pending_writes');"],
                    capture_output=True, text=True, timeout=2)
                result['mysqlStatus'] = dict(line.split('\t', 1) for line in r.stdout.splitlines()) if r.returncode == 0 else {'error': 'query_failed'}
            except subprocess.TimeoutExpired:
                result['mysqlStatus'] = {'error': 'timeout'}
        self.calls += 1
        return result
