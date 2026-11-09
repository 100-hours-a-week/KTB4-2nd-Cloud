#!/usr/bin/env python3
"""Prepare private templates or check local migration metadata. Never executes SQL/AWS."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from v2_v3_migration_contract import (ROOT, template, load_records, sql_checksums,
                                      preflight, counts_sql, compare_counts, repository_bindings)


def private_write(path, text):
    # Exclusive creation prevents replacing an existing evidence file or following a symlink.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write(text)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    init = commands.add_parser('init', help='Create a new private directory; existing paths refused')
    init.add_argument('--directory', type=Path, required=True)
    check = commands.add_parser('check', help='Check current local source/target JSON-lines evidence')
    check.add_argument('--directory', type=Path, required=True)
    check.add_argument('--sql-directory', type=Path, required=True, help='V1-V35 SQL, verified against repository source.lock')
    check.add_argument('--report', type=Path)
    counts = commands.add_parser('counts-sql', help='Print read-only exact count SQL from a metadata table catalog')
    counts.add_argument('--metadata', type=Path, required=True)
    compare = commands.add_parser('compare-counts', help='Compare exact business row counts, not row content')
    for name in ['before','after','source-metadata','target-metadata']:
        compare.add_argument('--'+name, type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == 'init':
            args.directory.mkdir(mode=0o700)
            private_write(args.directory/'inventory.json', json.dumps(template(), indent=2)+'\n')
            private_write(args.directory/'mysql-metadata.sql', (ROOT/'scripts/migration/mysql-metadata.sql').read_text())
            print(json.dumps({'templateCreated': True, 'readyToMigrate': False, 'resourcesChanged': False}))
            return 0
        if args.command == 'counts-sql':
            print(counts_sql(load_records(args.metadata)), end='')
            return 0
        if args.command == 'compare-counts':
            source = load_records(args.source_metadata)
            target = load_records(args.target_metadata)
            result = compare_counts(load_records(args.before), load_records(args.after),
                                    {r['table'] for r in source if r['kind']=='table'},
                                    {r['table'] for r in target if r['kind']=='table'})
            print(json.dumps(result))
            return 0 if result['rowCountsMatch'] else 2
        repository_bindings()
        expected = sql_checksums(args.sql_directory)
        evidence = []
        for name in ['source.jsonl','target.jsonl']:
            path = args.directory/name
            evidence.append(load_records(path) if path.exists() else None)
        inventory_path = args.directory/'inventory.json'
        inventory = None
        if inventory_path.exists():
            if inventory_path.is_symlink() or inventory_path.stat().st_size > 1024*1024:
                raise ValueError('Invalid inventory')
            inventory = json.loads(inventory_path.read_text())
        result = preflight(*evidence, inventory, expected, datetime.now(timezone.utc))
        text = json.dumps(result, indent=2)+'\n'
        if args.report:
            private_write(args.report, text)
        print(text, end='')
        return 0 if result['metadataChecksPassed'] else 2
    except (ValueError, KeyError, TypeError, IndexError, OSError, UnicodeError):
        # Never echo input fragments, file paths, user rows or exception payloads.
        print(json.dumps({'error': 'INVALID_OR_UNAVAILABLE_LOCAL_EVIDENCE',
                          'readyToMigrate': False, 'resourcesChanged': False}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
