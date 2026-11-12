#!/usr/bin/env python3
"""Print an opt-in candidate to stdout. No network, writes, credentials or live activation."""
import argparse
import json
from pathlib import Path
from v3_autoscaling import prepare_candidate
p=argparse.ArgumentParser()
p.add_argument('--candidate',required=True,type=Path)
p.add_argument('--converter',action='store_true')
a=p.parse_args()
print(json.dumps(prepare_candidate(json.loads(a.candidate.read_text()),a.converter),indent=2))
