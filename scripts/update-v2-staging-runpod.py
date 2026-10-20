#!/usr/bin/env python3
"""Compatibility entry point for the original staging-only CLI."""
from pathlib import Path
import runpy
import sys

if __name__ == '__main__':
    if any(v == '--environment' or v.startswith('--environment=') for v in sys.argv[1:]):
        raise SystemExit('Use update-v2-runpod.py for an explicit environment')
    sys.argv[1:1] = ['--environment', 'staging']
    runpy.run_path(str(Path(__file__).with_name('update-v2-runpod.py')), run_name='__main__')
