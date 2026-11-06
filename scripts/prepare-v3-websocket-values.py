#!/usr/bin/env python3
"""Prepare reference-only WebSocket inputs from public metadata and a Redis public CA."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('ws_access', ROOT / 'scripts/check-v3-websocket.py')
access = importlib.util.module_from_spec(spec);spec.loader.exec_module(access)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True)
    parser.add_argument('--source', required=True)
    parser.add_argument('--origin', required=True)
    parser.add_argument('--configuration-revision', required=True)
    parser.add_argument('--redis-ca', required=True, type=Path)
    parser.add_argument('--directory', required=True, type=Path)
    args = parser.parse_args();os.umask(0o077)
    access.require(re.fullmatch(r'ghcr.io/100-hours-a-week/yeodam-websocket@sha256:[a-f0-9]{64}', args.image), 'Immutable WebSocket image required')
    access.require(re.fullmatch('[a-f0-9]{40}', args.source), 'Actual WebSocket image source required')
    access.require(re.fullmatch('[a-f0-9]{64}', args.configuration_revision), 'Revision must hash public CA and Secret VersionId metadata, never credential values')
    access.require(re.fullmatch(r'https://[A-Za-z0-9.-]+(:[0-9]+)?', args.origin), 'Explicit HTTPS origin required')
    access.require(not args.directory.exists(), 'Refusing to overwrite existing release inputs')
    ca = access.supply.public_ca(args.redis_ca.read_text())
    app = {'images':{'websocket':args.image},'defaults':{'configurationRevision':args.configuration_revision,'configMapData':{'RELEASE':args.source,'WEBSOCKET_ALLOWED_ORIGINS':args.origin}},'workloads':{'trip-websocket':{'enabled':True}}}
    args.directory.mkdir(parents=True)
    for name, value in [('apps.values.json', app), ('access.values.json', {'enabled':True,'redisCa':ca})]:
        with (args.directory / name).open('x') as file: json.dump(value, file, indent=2)
    print('Prepared public WebSocket inputs. Verify image, Secrets and peers before applying; no deployment performed.')


if __name__ == '__main__': main()
