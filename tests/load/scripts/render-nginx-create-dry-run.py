#!/usr/bin/env python3
"""Reconstruct #430 graphs from saved server timestamps; not a live Grafana screenshot."""
import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.dates as dates
import matplotlib.pyplot as plt


def main():
    p = argparse.ArgumentParser(description=__doc__); p.add_argument('--evidence', required=True, type=Path)
    root = p.parse_args().evidence
    data = json.loads((root/'analysis.json').read_text())
    access = [json.loads(x) for x in (root/'capture/app/access.jsonl').read_text().splitlines()]
    samples = [json.loads(x) for x in (root/'capture/app/samples.jsonl').read_text().splitlines()]
    kst = timezone(timedelta(hours=9))
    fig, axes = plt.subplots(3, 1, figsize=(12, 8), sharex=True, constrained_layout=True)
    create = [x for x in access if x['method'] == 'POST' and x['normalized_route'] == '/api/trips']
    buckets = {v: Counter(int(float(x['msec'])) for x in create if x['limit_req_status'] == v)
               for v in ['PASSED', 'REJECTED_DRY_RUN']}
    seconds = sorted({int(float(x['msec'])) for x in create})
    at = [datetime.fromtimestamp(x, kst) for x in seconds]
    bottom = [buckets['PASSED'][x] for x in seconds]
    axes[0].bar(at, bottom, width=.85/86400, color='#39896e', label='PASSED')
    axes[0].bar(at, [buckets['REJECTED_DRY_RUN'][x] for x in seconds], bottom=bottom,
                width=.85/86400, color='#d98936', label='REJECTED_DRY_RUN (still forwarded)')
    axes[0].set_ylabel('Create POST / second'); axes[0].legend(loc='upper left')
    axes[0].set_title(f"{len(create)} create responses: {sum(buckets['PASSED'].values())} PASSED / "
                      f"{sum(buckets['REJECTED_DRY_RUN'].values())} would reject; counts combine peers")
    stamp = [datetime.fromisoformat(x['utc']) for x in samples]
    axes[1].plot(stamp, [x.get('cpuPercent') for x in samples], color='#356fa0', linewidth=1)
    axes[1].set_ylabel('App CPU (%)'); axes[1].set_ylim(0, 105)
    peak = max(x.get('cpuPercent') or 0 for x in samples)
    axes[1].set_title(f'Host CPU peak {peak:.1f}%; no capacity/headroom claim')
    for label, marker, color, selected in [
        ('Create 201', '.', '#8f5e95', create),
        ('Read 200', '.', '#39896e', [x for x in access if x['method'] == 'GET' and x['status'] == 200 and x.get('limit_req_status') == 'PASSED'])]:
        axes[2].scatter([datetime.fromtimestamp(float(x['msec']), kst) for x in selected],
                        [float(x['request_time'])*1000 for x in selected], s=10, marker=marker, color=color, label=label)
    axes[2].set_ylabel('Nginx request ms'); axes[2].legend(loc='upper left')
    axes[2].set_title('Server request time for metadata and reads; photo request duration excluded')
    for stage in data['stages']:
        if stage['mode'] == 'rate' and stage['rate'] == 5:
            for ax in axes:
                ax.axvspan(datetime.fromisoformat(stage['startedAt']), datetime.fromisoformat(stage['endedAt']), color='#f0c689', alpha=.16)
    axes[2].xaxis.set_major_formatter(dates.DateFormatter('%H:%M:%S', tz=kst))
    axes[2].set_xlabel('2026-10-26 KST / shaded: 5 RPS stage with browser journeys from a different peer')
    for ax in axes:
        ax.grid(axis='y', alpha=.2); ax.spines[['top', 'right']].set_visible(False)
    fig.suptitle(f'V1 staging creation Dry Run / saved raw logs + {len(samples)} one-second samples / retrospective reconstruction')
    fig.savefig(root/'dry-run-timeline.png', dpi=160); plt.close(fig)


if __name__ == '__main__':
    main()
