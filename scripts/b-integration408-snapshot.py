#!/usr/bin/env python3
"""Pace only the rehearsal dump pipe; preserve snapshot coordinates and bytes."""
import argparse
import json
from pathlib import Path
import sys
import time
from uuid import UUID


def transfer(source, destination, rate, clock=time.monotonic, sleep=time.sleep):
    if rate < 0:
        raise ValueError('Negative dump rate')
    started = clock()
    total = 0
    while True:
        block = source.read(65536)
        if not block:
            break
        if rate:
            remaining = (total + len(block)) / rate - (clock() - started)
            if remaining > 0:
                sleep(remaining)
        destination.write(block)
        destination.flush()
        total += len(block)
    elapsed = clock() - started
    return {'uncompressedBytes': total, 'streamSeconds': elapsed,
            'configuredBytesPerSecond': rate,
            'observedBytesPerSecond': total / elapsed if elapsed else None}


def render(template, run, rate):
    if str(UUID(run)) != run or not isinstance(rate, int) or rate < 0:
        raise ValueError('Invalid snapshot contract')
    original = '  | gzip -1 \\\n'
    if template.count(original) != 1:
        raise ValueError('Snapshot pipeline changed; inspect before execution')
    pipe = ('  | python3 /run/yeodam-b391/b-integration408-snapshot.py stream'
            ' --run-id ' + run + ' --rate ' + str(rate) + ' \\\n'
            '  | gzip -1 \\\n')
    # All other commands/options, coordinate capture and checksum stay identical.
    return template.replace(original, pipe)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('stream', 'render'))
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--rate', type=int, required=True)
    parser.add_argument('--template', type=Path)
    args = parser.parse_args()
    if str(UUID(args.run_id)) != args.run_id or args.rate < 0:
        raise ValueError('Invalid Run/rate')
    if args.action == 'render':
        print(render(args.template.read_text(), args.run_id, args.rate), end='')
    else:
        directory = Path('/run/yeodam-b391') / args.run_id
        # A stream cannot create a new arbitrary Run or overwrite an old result.
        output = directory / 'snapshot-stream.json'
        if not directory.is_dir() or output.exists():
            raise RuntimeError('Expected prepared Run without previous stream evidence')
        result = transfer(sys.stdin.buffer, sys.stdout.buffer, args.rate)
        with output.open('x') as file:
            json.dump(result, file)
        output.chmod(0o600)


if __name__ == '__main__':
    main()
