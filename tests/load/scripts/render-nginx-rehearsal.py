#!/usr/bin/env python3
"""Offline PNG/CSV from archived data. No Grafana/Prometheus access or credential required."""
import argparse
from collections import Counter
import csv
from datetime import datetime, timezone
import hashlib
import importlib.util
import secrets
import json
import math
import os
from pathlib import Path
import tempfile


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--archive', type=Path, required=True)
    p.add_argument('--output', type=Path)
    p.add_argument('--journey', type=Path)
    p.add_argument('--audit', type=Path)
    a = p.parse_args()
    if bool(a.journey) != bool(a.audit):
        p.error('Attach journey results and DB/S3 audit together')
    target = a.output or a.archive/'plots'
    target.mkdir(exist_ok=False)
    os.environ.setdefault('MPLCONFIGDIR', tempfile.mkdtemp(prefix='nginx426-plot-'))
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import matplotlib.dates as dates
    plt.rcParams.update({'font.size': 9, 'axes.spines.top': False, 'axes.spines.right': False})
    has_host = (a.archive/'app/samples.jsonl').exists()
    rows = [json.loads(line) for line in (a.archive/'app/samples.jsonl').read_text().splitlines()] if has_host else []
    marks = [json.loads(line) for line in (a.archive/'app/marks.jsonl').read_text().splitlines()] if has_host else []
    fields = ['utc', 'schedulingLagSeconds', 'active', 'reading', 'writing', 'waiting', 'httpsEstablished',
              'cpuPercent', 'memoryUsedPercent', 'diskPercent', 'nginxFdTotal']
    with (target/'host.csv').open('w') as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)
    times = [datetime.fromisoformat(r['utc']) for r in rows]
    def annotate(axis):
        axis.xaxis.set_major_formatter(dates.DateFormatter('%H:%M:%S', tz=timezone.utc))
        axis.grid(alpha=.2)
        for mark in marks:
            at = datetime.fromisoformat(mark['utc'])
            axis.axvline(at, color='#888888', linestyle='--', linewidth=.7)
    if has_host:
        fig, axes = plt.subplots(3, 2, figsize=(13, 8), constrained_layout=True)
        groups = [('Nginx client connections', ['active', 'waiting', 'reading', 'writing']),
                  ('HTTPS Established (1s samples)', ['httpsEstablished']),
                  ('Host CPU / memory (%)', ['cpuPercent', 'memoryUsedPercent']),
                  ('Nginx open FDs (not connection slots)', ['nginxFdTotal']),
                  ('Root disk used (%)', ['diskPercent']),
                  ('Collector scheduling lag (seconds)', ['schedulingLagSeconds'])]
        for axis, (title, keys) in zip(axes.flat, groups):
            for key in keys:
                values = [r.get(key) if r.get(key) is not None and (i == 0 or (times[i]-times[i-1]).total_seconds()<=2.5) else math.nan for i,r in enumerate(rows)]
                axis.plot(times, values, label=key, linewidth=1.1)
            axis.set_title(title)
            if '%' in title:
                axis.set_ylim(0,100)
            elif 'FDs' in title:
                axis.set_ylim(bottom=0,top=max(128,max(r.get('nginxFdTotal',0) for r in rows)*1.15))
            else:
                axis.set_ylim(bottom=0)
            axis.legend(loc='upper left', fontsize=7)
            annotate(axis)
        fig.suptitle('V1 staging rehearsal | UTC | '+a.archive.name)
        fig.savefig(target/'host.png', dpi=170)
        plt.close(fig)
    index = json.loads((a.archive/'plg/index.json').read_text())
    for query in index['queries']:
        path = a.archive/'plg'/query['file']
        if not path.exists():
            continue
        data = json.loads(path.read_text())['data']['result']
        filename = Path(query['file']).stem
        with (target/(filename+'.csv')).open('w') as f:
            writer = csv.writer(f)
            writer.writerow(['seriesLabels', 'utcUnixSeconds', 'value'])
            for series in data:
                for at, value in series.get('values', []):
                    writer.writerow([json.dumps(series.get('metric', {}), sort_keys=True), at, value])
        fig, axis = plt.subplots(figsize=(10, 3), constrained_layout=True)
        for series in data[:8]:
            values = series.get('values', [])
            label = json.dumps(series.get('metric', {}), sort_keys=True)
            axis.plot([datetime.fromtimestamp(float(x), timezone.utc) for x, y in values],
                      [float(y) for x, y in values], linewidth=1, label=label[:100])
        axis.set_title(query['title']+' | '+query['engine']+' | original step15s')
        annotate(axis)
        if not data:
            axis.text(.5, .5, 'No stored series (not proof of zero)', ha='center', transform=axis.transAxes)
        elif len(data) <= 3:
            axis.legend(fontsize=6)
        if len(data) > 8:
            fig.supxlabel('First8 of'+str(len(data))+' series plotted; all series retained in JSON/CSV')
        fig.savefig(target/(filename+'.png'), dpi=150)
        plt.close(fig)
    access = [json.loads(x) for x in (a.archive/'app/access.jsonl').read_text().splitlines()] if has_host else []
    loki = [json.loads(x)['data'] for x in (a.archive/'plg/loki-access.jsonl').read_text().splitlines()]
    expected = Counter(r['request_id'] for r in access if r.get('request_id'))
    found = Counter(r['request_id'] for r in loki if r.get('request_id'))
    normal = a.archive/'app/normal-summary.json'
    report = {'httpStatuses': dict(Counter(str(r.get('status')) for r in access)),
              'hostCaptureAvailable':has_host,'appRequestIds': sum(expected.values()) if has_host else None, 'missingInLoki': sum((expected-found).values()) if has_host else None,
              'extraInLoki': sum((found-expected).values()) if has_host else None,
              'normalResult': json.loads(normal.read_text()) if normal.exists() else None,
              'normalJourneyAndDbS3AuditAttached': False, 'matplotlibVersion': matplotlib.__version__,
              'sourceHashes': {str(p.relative_to(a.archive)): hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in [a.archive/'app/samples.jsonl', a.archive/'plg/index.json'] if p.exists()}}
    if a.journey:
        events = [json.loads(x) for x in (a.journey/'events.jsonl').read_text().splitlines()]
        completed = next((e for e in events if e.get('event') == 'run_completed'), None)
        audit = json.loads(a.audit.read_text())
        if not completed or completed.get('tripId') != audit.get('tripId'):
            raise ValueError('Journey and audit identities differ')
        report['normalJourneyAndDbS3AuditAttached'] = True
        capture_index = json.loads((a.archive/('app/index.json' if has_host else 'run-index.json')).read_text())
        report['journeyInsideCapture'] = (datetime.fromisoformat(capture_index['from'])
            <= datetime.fromisoformat(completed['startedAt'].replace('Z','+00:00'))
            <= datetime.fromisoformat(completed['endedAt'].replace('Z','+00:00'))
            <= datetime.fromisoformat(capture_index['to']))
        report['journeyPass'] = completed.get('result') == 'success' and report['journeyInsideCapture']
        report['dataAuditPass'] = (audit.get('processingStatus') == 'COMPLETED'
            and audit.get('dbAttachments') == 150 and audit.get('s3ObjectsFound') == 150
            and audit.get('s3SizeMultisetMatchesManifest') is True)
        spec = importlib.util.spec_from_file_location('archive_render', Path(__file__).with_name('nginx-rehearsal-archive.py'))
        sanitizer = importlib.util.module_from_spec(spec); spec.loader.exec_module(sanitizer)
        key = secrets.token_bytes(32)
        for name, value in [('journey-events.json', events), ('journey-audit.json', audit),
                            ('journey-summary.json', json.loads((a.journey/'summary.json').read_text()))]:
            (target/name).write_text(json.dumps(sanitizer.scrub(value,key), indent=2))
    (target/'analysis.json').write_text(json.dumps(report, indent=2))
    print(json.dumps({'plots': len(list(target.glob('*.png'))), 'analysis': report}))


if __name__ == '__main__':
    main()
