#!/usr/bin/env python3
"""Retrospective PNG/CSV from #428 raw evidence, not a synthetic or live screenshot."""
import argparse
import csv
from datetime import datetime, timezone
import importlib.util
import json
import math
from pathlib import Path


def main():
    p = argparse.ArgumentParser(description=__doc__); p.add_argument('--evidence', type=Path, required=True)
    a = p.parse_args(); root = a.evidence; out = root/'plots'; out.mkdir(exist_ok=True)
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import matplotlib.dates as dates
    s = importlib.util.spec_from_file_location('analysis', Path(__file__).with_name('analyze-nginx-create.py'))
    m = importlib.util.module_from_spec(s); s.loader.exec_module(m)
    analysis = json.loads((root/'analysis.json').read_text()); stages = sorted(analysis['stages'], key=lambda x: x['startedAt'])
    rates = [x for x in stages if x['mode'] == 'rate']
    rows = sorted([v for f in root.glob('capture-*/app/samples.jsonl') for v in m.records(f)], key=lambda x: x['utc'])
    rows = [x for x in rows if x['utc'] >= rates[0]['startedAt']]
    events = [v for folder in (root/'generator').iterdir() if folder.is_dir() for v in m.records(folder/'events.jsonl')]
    finals = [x for x in events if x.get('event') == 'final' and x.get('status') == 201]
    normal = [x for x in events if x.get('event') == 'normal' and x.get('status') == 200]
    first_ms = m.milliseconds(rates[0]['startedAt'])
    finals = [x for x in finals if x['atMs'] >= first_ms]
    normal = [x for x in normal if x['atMs'] >= first_ms]
    def dt(value):
        return datetime.fromisoformat(value.replace('Z', '+00:00'))
    plt.rcParams.update({'font.size': 9, 'axes.spines.top': False, 'axes.spines.right': False})
    fig, axes = plt.subplots(4, 1, figsize=(13, 11), sharex=True, constrained_layout=True)
    times = [dt(x['utc']) for x in rows]
    def gaps(values):
        return [math.nan if v is None or (i and (times[i]-times[i-1]).total_seconds() > 2.5) else v for i, v in enumerate(values)]
    axes[0].plot(times, gaps([x.get('cpuPercent') for x in rows]), color='#c63e3e', label='App host non-idle CPU (%)')
    axes[0].set_ylim(0, 105); axes[0].set_ylabel('Host CPU %')
    for name, color in [('mysqld', '#196ca3'), ('java', '#cd7a1a'), ('nginx', '#5d8c38')]:
        series = []
        for x in rows:
            values = [v['cpuPercentOneCore'] for v in x.get('processes', []) if v['name'] == name and v.get('cpuPercentOneCore') is not None]
            series.append(sum(values) if values else None)
        axes[1].plot(times, gaps(series), label=name, color=color, linewidth=1)
    axes[1].set_ylabel('Process CPU %\n100% = one core'); axes[1].set_ylim(bottom=0)
    for points, label, color in [(finals, 'Create client elapsed (201 only)', '#307bb0'), (normal, 'Normal read elapsed (200 only)', '#56a445')]:
        axes[2].scatter([datetime.fromtimestamp(x['atMs']/1000, timezone.utc) for x in points], [x['elapsedMs'] for x in points], s=4, alpha=.55, label=label, color=color)
    axes[2].axhline(1000, linestyle='--', color='#bd3d3d', linewidth=1, label='1s initial test guard (not SLO)'); axes[2].set_ylabel('Client elapsed ms'); axes[2].set_ylim(bottom=0)
    axes[3].plot(times, gaps([x.get('nginxFdTotal') for x in rows]), label='Nginx FDs (sum)', color='#8455a7')
    axes[3].plot(times, gaps([x.get('httpsEstablished') for x in rows]), label='HTTPS established TCP', color='#527d89'); axes[3].set_ylabel('Count (different objects)'); axes[3].set_ylim(bottom=0)
    for axis in axes:
        for stage in rates:
            axis.axvspan(dt(stage['startedAt']), dt(stage['endedAt']), alpha=.055, color='#d84646' if stage['exitCode'] else '#297bb8')
        axis.legend(loc='upper left', fontsize=8); axis.grid(alpha=.2)
    for stage in rates:
        axes[0].text(dt(stage['startedAt']), 102, str(stage['rate'])+' RPS', fontsize=8, rotation=0, va='top')
    axes[3].xaxis.set_major_formatter(dates.DateFormatter('%H:%M:%S', tz=timezone.utc)); axes[3].set_xlabel('2026-10-26 UTC (KST = UTC +9)')
    fig.suptitle('V1 staging creation | fixed HTTP/2 input, 3 synthetic users | one run per stage, gaps = not captured\n50 RPS first run passed; 75 and 50 repeat aborted; 30 repeat passed. No connection-slot errors.', fontsize=12)
    fig.savefig(out/'stages-and-resources.png', dpi=170); plt.close(fig)
    fields=['rate','started','confirmed','dbRows','withoutConfirmedResponse','p95Ms','maxMs','normal200','normalAbortStatus0','appCpuMedian','appCpuMax']
    with (out/'comparison.csv').open('w') as f:
        w=csv.DictWriter(f,fieldnames=fields);w.writeheader()
        for r in rates:
            q=r['reconciliation'];w.writerow(dict(zip(fields,[r['rate'],q['startedEvents'],q['confirmedEvents'],r['stageDbRows'],q['committedWithoutConfirmedResponse'],r['create']['p(95)'],r['create']['max'],r['normalRequests']-r['normalZeroStatusOnAbort']-r['normalHttpFailures'],r['normalZeroStatusOnAbort'],r['appCpuMedian'],r['appCpuMax']])))
    fig, ax = plt.subplots(figsize=(10, 4), constrained_layout=True)
    labels=[str(x['rate'])+(' repeat' if any(y['rate'] == x['rate'] for y in rates[:i]) else '') for i, x in enumerate(rates)]; ax.bar(labels,[x['create']['p(95)'] for x in rates],color=['#3677a5' if not x['exitCode'] else '#bf4747' for x in rates]);ax.set_xlabel('Offered creation RPS');ax.set_ylabel('Client elapsed p95 ms (received responses)');ax.set_title('Same payload / fixed 200 VUs / retained data grows between stages\n75 RPS stopped early; unconfirmed commits are counted separately');ax.grid(axis='y',alpha=.2);fig.savefig(out/'latency-comparison.png',dpi=170);plt.close(fig)
    (out/'provenance.json').write_text(json.dumps({'retrospective':True,'source':'raw #428 sample/event files, analysis.json','matplotlibVersion':matplotlib.__version__},indent=2))
    print(json.dumps({'plots':2,'csv':str(out/'comparison.csv'),'retrospective':True}))


if __name__ == '__main__':
    main()
