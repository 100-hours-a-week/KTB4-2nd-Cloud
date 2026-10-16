#!/usr/bin/env python3
"""Render PromQL/pending-window tests; run promtool test rules in the output directory."""
import json
from pathlib import Path
import sys
specs = json.loads(Path(sys.argv[1]).read_text())
output = Path(sys.argv[2]); output.mkdir(parents=True, exist_ok=True)
uids = {'v2s-gpu-agent','v2s-gpu-runtime','v2s-http-errors','v2s-scrape-backend'}
rules = [{'alert': s['uid'], 'expr': '(' + s['expr'] + ') > 0', 'for': s['hold']}
         for s in specs if s['uid'] in uids]
(output/'rules.json').write_text(json.dumps({'groups':[{'name':'v2-staging','rules':rules}]}))
fixture = json.loads(Path(sys.argv[3]).read_text())
(output/'tests.json').write_text(json.dumps(dict(fixture, rule_files=['rules.json'])))
