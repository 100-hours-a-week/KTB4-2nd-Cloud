#!/usr/bin/env python3
"""Read a fixed set of NVIDIA/container/readiness stats; never read container envs or job data."""
import argparse
import csv
import io
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile
import time
import urllib.request

COMPONENTS = {'embedding': 'yeodam-ai-embedding', 'qdrant': 'yeodam-ai-qdrant', 'process-runner': 'yeodam-ai-process-runner'}


def nvidia_metrics(output):
    rows = []
    for index, row in enumerate(csv.reader(io.StringIO(output))):
        if len(row) != 3:
            raise ValueError('Expected utilization, used MiB, total MiB')
        utilization, used, total = map(float, row)
        if not all(map(math.isfinite, (utilization, used, total))) or not 0 <= utilization <= 100 or not 0 <= used <= total or total <= 0:
            raise ValueError('Unsupported or invalid NVIDIA sample')
        labels = '{gpu="' + str(index) + '"}'
        rows.extend([f'yeodam_gpu_utilization_percent{labels} {utilization}',
                     f'yeodam_gpu_memory_used_bytes{labels} {used * 1024 * 1024}',
                     f'yeodam_gpu_memory_total_bytes{labels} {total * 1024 * 1024}'])
    if not rows:
        raise ValueError('No NVIDIA GPU sample')
    return rows


def run(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=10).stdout


def collect(address):
    rows = [f'yeodam_gpu_sample_timestamp_seconds {time.time()}']
    try:
        rows += nvidia_metrics(run('nvidia-smi', '--query-gpu=utilization.gpu,memory.used,memory.total', '--format=csv,noheader,nounits'))
        rows.append('yeodam_gpu_smi_success 1')
    except (subprocess.SubprocessError, ValueError, OSError):
        rows.append('yeodam_gpu_smi_success 0')
    for name, container in COMPONENTS.items():
        labels = '{component="' + name + '"}'
        try:
            info = json.loads(run('docker', 'inspect', '--format',
                                 '{"running":{{.State.Running}},"restarts":{{.RestartCount}}}', container))
            rows += [f'yeodam_ai_container_inspect_success{labels} 1',
                     f'yeodam_ai_container_running{labels} {int(info["running"])}',
                     f'yeodam_ai_container_restarts{labels} {int(info["restarts"])}']
        except (subprocess.SubprocessError, ValueError, KeyError, OSError):
            rows.append(f'yeodam_ai_container_inspect_success{labels} 0')
    for name, port, path in [('embedding', 8000, '/health'), ('qdrant', 6333, '/readyz')]:
        ready = 0
        try:
            with urllib.request.urlopen(f'http://{address}:{port}{path}', timeout=5) as response:
                ready = int(response.status == 200)
        except OSError:
            pass
        rows.append('yeodam_ai_endpoint_ready{component="' + name + '"} ' + str(ready))
    # Two known filesystems, not the Docker overlay or arbitrary application paths.
    for path in ['/', '/data/qdrant']:
        try:
            if path != '/' and not os.path.ismount(path):
                rows.append('yeodam_ai_filesystem_mounted{mountpoint="' + path + '"} 0')
                continue
            stat = os.statvfs(path)
            labels = '{mountpoint="' + path + '"}'
            rows += [f'yeodam_ai_filesystem_mounted{labels} 1',
                     f'yeodam_ai_filesystem_available_bytes{labels} {stat.f_bavail * stat.f_frsize}',
                     f'yeodam_ai_filesystem_size_bytes{labels} {stat.f_blocks * stat.f_frsize}']
        except OSError:
            rows.append('yeodam_ai_filesystem_mounted{mountpoint="' + path + '"} 0')
    return '\n'.join(rows) + '\n'


def write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as file:
            temporary = file.name
            file.write(content)
            os.fchmod(file.fileno(), 0o644)
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--address', required=True)
    parser.add_argument('--output', type=Path, default=Path('/var/lib/yeodam/gpu-metrics/gpu.prom'))
    args = parser.parse_args()
    import ipaddress
    if ipaddress.ip_address(args.address) not in ipaddress.ip_network('10.30.0.0/16'):
        raise SystemExit('Expected staging private address')
    write(args.output, collect(args.address))
