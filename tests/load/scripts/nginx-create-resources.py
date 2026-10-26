#!/usr/bin/env python3
"""Optional #428 process/DB sampling. No SQL text, environment or process command arguments."""
import os
from pathlib import Path
import subprocess
import time

NAMES = {'nginx', 'java', 'mysqld', 'alloy', 'k6'}
CPU_MODES = ['user', 'nice', 'system', 'idle', 'iowait', 'irq', 'softirq', 'steal']
CONTAINERS = ['yeodam-app-frontend-1', 'yeodam-app-backend-1']


def cpu_modes(previous, current):
    """Guest ticks are already counted in user/nice; never add them again."""
    if previous is None or len(current) != 8 or len(previous) != 8:
        return None
    delta = [a-b for a, b in zip(current, previous)]
    total = sum(delta)
    if total <= 0 or min(delta) < 0:
        return None
    return {k: round(v/total*100, 3) for k, v in zip(CPU_MODES, delta)}


def cgroup_path(pid):
    lines = (Path('/proc')/str(pid)/'cgroup').read_text().splitlines()
    relative = next(x.split(':', 2)[2] for x in lines if x.startswith('0::'))
    root = Path('/sys/fs/cgroup').resolve()
    path = (root/relative.lstrip('/')).resolve()
    if not path.is_relative_to(root) or path == root:
        raise ValueError('Expected a child cgroup v2')
    return path


def container_usage(previous, usage, seconds):
    if previous is None or usage < previous or seconds <= 0:
        return None
    return round((usage-previous)/1e6/seconds*100, 3)


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
        self.previous_modes = None
        self.containers = {}
        self.container_previous = {}

    def discover_containers(self):
        result = subprocess.run(['docker', 'inspect', '--format', '{{.Id}} {{.Name}} {{.State.Pid}}', *CONTAINERS],
                                capture_output=True, text=True, timeout=2)
        result.check_returncode()
        found = {}
        for line in result.stdout.splitlines():
            identity, name, pid = line.split()
            if int(pid) <= 0:
                continue
            path = cgroup_path(pid)
            found[path] = {'id': identity, 'name': name.lstrip('/'), 'initPid': int(pid)}
        self.containers = found

    def sample(self):
        at = time.monotonic()
        seconds = at - self.at if self.at else 0
        metadata_error = None
        if self.calls % 30 == 0:
            try:
                self.discover_containers()
            except (OSError, ValueError, StopIteration, subprocess.SubprocessError) as error:
                metadata_error = type(error).__name__
        rows, current = [], {}
        for directory in Path('/proc').iterdir():
            if not directory.name.isdigit():
                continue
            try:
                name = (directory/'comm').read_text().strip()
                group = cgroup_path(directory.name)
                container = self.containers.get(group)
                if name not in NAMES and container is None:
                    continue
                stat = parse_stat((directory/'stat').read_text())
                current[directory.name] = stat
                limits = next(x.split()[-3:] for x in (directory/'limits').read_text().splitlines() if x.startswith('Max open files'))
                io = dict(x.split(':', 1) for x in (directory/'io').read_text().splitlines())
                rows.append({'pid': int(directory.name), 'name': name, 'cpuPercentOneCore': process_percent(
                    self.previous.get(directory.name), stat, seconds, self.hz),
                    'openFds': len(list((directory/'fd').iterdir())), 'fdSoftLimit': limits[0],
                    'readBytes': int(io['read_bytes']), 'writeBytes': int(io['write_bytes']),
                    'containerName': container['name'] if container else None})
            except (FileNotFoundError, ProcessLookupError, PermissionError, StopIteration, ValueError):
                continue
        self.previous, self.at = current, at
        result = {'processes': rows, 'processCpuUnit': '100%=one logical CPU; first/reused PID sample null'}
        modes = list(map(int, Path('/proc/stat').read_text().splitlines()[0].split()[1:9]))
        result['cpuModesPercent'] = cpu_modes(self.previous_modes, modes)
        self.previous_modes = modes
        containers, usage_now = [], {}
        for group, identity in self.containers.items():
            try:
                stats = dict(line.split() for line in (group/'cpu.stat').read_text().splitlines())
                usage = int(stats['usage_usec']); usage_now[identity['id']] = usage
                containers.append({**identity, 'cpuPercentOneCore': container_usage(
                    self.container_previous.get(identity['id']), usage, seconds),
                    'nrThrottled': int(stats.get('nr_throttled', 0)), 'throttledUsec': int(stats.get('throttled_usec', 0))})
            except (OSError, ValueError, KeyError):
                containers.append({**identity, 'error': 'cgroup_unavailable'})
        self.container_previous = usage_now
        result['containers'] = containers
        if metadata_error:
            result['containerDiscoveryError'] = metadata_error
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
