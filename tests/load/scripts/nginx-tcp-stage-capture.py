#!/usr/bin/env python3
"""Bounded, source-filtered TCP metadata capture on the exact V1 staging App. No payload output."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import selectors
import shutil
import signal
import subprocess
import time
from uuid import UUID

APP = 'i-05d320e3a87200d5a'
ORIGIN = 'https://v1-staging.yeodam-2gether.com'


def endpoint(text):
    address, port = text.rsplit('.', 1)
    return str(ipaddress.ip_address(address)), int(port)


def parse_packet(line, sources, key):
    match = re.match(r'^(\d+\.\d+)\s+IP6?\s+(\S+)\s+>\s+(\S+):\s+Flags \[([^\]]*)\],(.*)$', line)
    if not match:
        raise ValueError('Unsupported/truncated TCP summary')
    src, sp = endpoint(match[2]);dst, dp = endpoint(match[3])
    if src in sources and dp == 443:
        peer, port, direction = src, sp, 'in'
    elif dst in sources and sp == 443:
        peer, port, direction = dst, dp, 'out'
    else:
        raise ValueError('Packet outside the confirmed source filter')
    identity = hmac.new(key, peer.encode(), hashlib.sha256).hexdigest()[:16]
    flow = hmac.new(key, (peer+':'+str(port)).encode(), hashlib.sha256).hexdigest()[:20]
    length = re.search(r'\blength (\d+)', match[5])
    return {'utc': datetime.fromtimestamp(float(match[1]), timezone.utc).isoformat(),
            'epochSeconds': float(match[1]), 'family': ipaddress.ip_address(peer).version,
            'sourceId': identity, 'flowId': flow, 'clientPort': port, 'serverPort': 443,
            'direction': direction, 'flags': match[4],
            'tcpPayloadLength': int(length[1]) if length else None}


def validate(a):
    UUID(a.run_id)
    if a.confirm_target != ORIGIN or not 10 <= a.seconds <= 1800:
        raise ValueError('Exact staging origin and 10..1800 seconds required')
    if not 1 <= len(a.source_ip) <= 2:
        raise ValueError('One or two explicit normal-probe sources required')
    for raw in a.source_ip:
        ip = ipaddress.ip_address(raw)
        if ip.is_multicast or (not ip.is_global and raw != '10.30.0.126'):
            raise ValueError('Global probe source or exact existing generator only')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run-id', required=True)
    p.add_argument('--source-ip', action='append', required=True)
    p.add_argument('--seconds', type=int, required=True)
    p.add_argument('--confirm-target', required=True)
    p.add_argument('--execute', action='store_true')
    a = p.parse_args()
    try: validate(a)
    except (ValueError, TypeError) as error: p.error(str(error))
    if not a.execute:
        print(json.dumps({'execute': False, 'seconds': a.seconds, 'sourceCount': len(a.source_ip)}));return
    if os.geteuid() != 0 or Path('/var/lib/cloud/data/instance-id').read_text().strip() != APP:
        p.error('Exact existing V1 staging App root only')
    if shutil.disk_usage('/').used / shutil.disk_usage('/').total >= .8:
        p.error('Filesystem >=80%; no packet observation started')
    os.umask(0o077)
    root = Path('/var/lib/yeodam/nginx-rehearsal')/a.run_id
    if not (root/'plan.json').is_file(): p.error('Start the matching independent host capture first')
    output = root/'tcp445';output.mkdir(exist_ok=False)
    sources = {str(ipaddress.ip_address(x)) for x in a.source_ip}
    key = secrets.token_bytes(32)
    bpf = 'tcp port 443 and ('+' or '.join('host '+x for x in sorted(sources))+')'
    plan = {'runId': a.run_id, 'startedAt': datetime.now(timezone.utc).isoformat(), 'seconds': a.seconds,
            'interface': 'ens5', 'snaplen': 96, 'promiscuous': False, 'sourceCount': len(sources),
            'filterSha256': hashlib.sha256(bpf.encode()).hexdigest(), 'maxPackets': 100000,
            'sourceIds': [hmac.new(key, ip.encode(), hashlib.sha256).hexdigest()[:16] for ip in sorted(sources)],
            'maxOutputBytes': 32*1024**2, 'payloadStored': False,
            'scope': 'TCP flags/length metadata only; TLS stage requires client timings/errors; no decryption'}
    (output/'plan.json').write_text(json.dumps(plan, indent=2))
    interrupted = False
    def stop(*_):
        nonlocal interrupted
        interrupted = True
    for sig in [signal.SIGTERM, signal.SIGINT]: signal.signal(sig, stop)
    flags = Counter();packets = errors = discarded = written = 0
    reason = None;stop_at = None;buffer = b''
    began = time.monotonic();last_checkpoint = 0
    with (output/'tcpdump-stats.txt').open('wb') as err, (output/'packets.jsonl').open('x') as out:
        process = subprocess.Popen(['tcpdump', '-p', '-i', 'ens5', '-nn', '-tt', '-l',
                                    '-s', '96', '--immediate-mode', bpf], stdout=subprocess.PIPE, stderr=err)
        selector = selectors.DefaultSelector();selector.register(process.stdout, selectors.EVENT_READ)
        def consume(raw):
            nonlocal packets, errors, discarded, written, reason
            if not raw.strip(): return
            if reason in ['packet-budget', 'byte-budget']:
                discarded += 1;return
            try: row = parse_packet(raw.decode('ascii'), sources, key)
            except (ValueError, UnicodeError):
                errors += 1;return  # Never retain a raw line/IP on a parser error.
            encoded = json.dumps(row)+'\n'
            if written+len(encoded) > plan['maxOutputBytes']:
                reason = 'byte-budget';discarded += 1;return
            out.write(encoded);out.flush();written += len(encoded);packets += 1
            flags[row['direction']+':'+row['flags']] += 1
            if packets >= plan['maxPackets']: reason = 'packet-budget'
        try:
            while selector.get_map():
                if reason is None and (interrupted or (output/'STOP').exists()): reason = 'requested-stop'
                if reason is None and time.monotonic()-began >= a.seconds: reason = 'duration'
                if reason and stop_at is None:
                    if process.poll() is None: process.send_signal(signal.SIGINT)
                    stop_at = time.monotonic()
                if stop_at and time.monotonic()-stop_at > 3 and process.poll() is None:
                    process.kill();reason = 'forced-kill'
                for event, _ in selector.select(.1):
                    chunk = os.read(event.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(event.fileobj);break
                    buffer += chunk
                    while b'\n' in buffer:
                        line, buffer = buffer.split(b'\n', 1);consume(line)
                if not reason and process.poll() is not None: reason = 'tcpdump-exit'
                state = {**plan, 'updatedAt': datetime.now(timezone.utc).isoformat(), 'packets': packets,
                         'parseErrors': errors, 'discardedAfterBudget': discarded, 'reason': reason,
                         'bytesWritten': written, 'flags': dict(flags)}
                if time.monotonic()-last_checkpoint >= 1:
                    tmp = output/'checkpoint.tmp';tmp.write_text(json.dumps(state));tmp.replace(output/'checkpoint.json')
                    last_checkpoint = time.monotonic()
        finally:
            if process.poll() is None: process.kill()
            process.wait(timeout=5);selector.close()
        if buffer: consume(buffer)
    stats = (output/'tcpdump-stats.txt').read_text(errors='replace')
    # Statistics may be absent on startup failure; absence is not zero loss.
    dropped = re.search(r'(\d+) packets dropped by kernel', stats)
    state.update(packets=packets, parseErrors=errors, discardedAfterBudget=discarded,
                 bytesWritten=written, flags=dict(flags),
                 endedAt=datetime.now(timezone.utc).isoformat(), exitCode=process.returncode,
                 kernelDropped=int(dropped[1]) if dropped else None,
                 complete=packets > 0 and errors == 0 and discarded == 0 and dropped is not None
                 and int(dropped[1]) == 0 and process.returncode == 0
                 and reason in ['duration', 'requested-stop'])
    (output/'summary.json').write_text(json.dumps(state, indent=2))
    print(json.dumps(state))
    return 0 if state['complete'] else 1


if __name__ == '__main__': raise SystemExit(main())
