#!/usr/bin/env python3
"""Prepare a private sampled request header or inspect its local Tempo evidence.

This tool sends no application request and changes no sampling/deployment setting.
"""
import argparse
import json
import os
from pathlib import Path
from v3_workflow_trace import trial, trace_id, read_tempo, inspect_trace


def private_write(path, value):
    if path.parent.is_symlink() or path.parent.stat().st_mode & 0o077:
        raise ValueError('Output parent must be an existing private directory (mode 700)')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, indent=2)
        stream.write('\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    new = sub.add_parser('prepare');new.add_argument('--output', type=Path, required=True)
    check = sub.add_parser('inspect');check.add_argument('--manifest', type=Path, required=True)
    check.add_argument('--tempo', default='http://127.0.0.1:3200')
    check.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.action == 'prepare':
        private_write(args.output, trial())
        print('Private sampled header prepared; no application request sent')
        return
    if args.manifest.is_symlink() or args.manifest.stat().st_size > 4096:
        raise ValueError('Invalid manifest file')
    tid = trace_id(json.loads(args.manifest.read_text()))
    result = inspect_trace(read_tempo(args.tempo, tid), tid)
    private_write(args.output, result)
    print(json.dumps({key:result[key] for key in ('status','tracePathComplete')}))
    if not result['tracePathComplete']:
        raise SystemExit(2)


if __name__ == '__main__':
    main()
