#!/usr/bin/env python3
"""Offline #440 raw evidence analysis and Korean publication figures."""
import argparse
from collections import Counter
from datetime import datetime
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
from zoneinfo import ZoneInfo

spec = importlib.util.spec_from_file_location('load440', Path(__file__).with_name('nginx-http-tls-load.py'))
load = importlib.util.module_from_spec(spec)
spec.loader.exec_module(load)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--archive', required=True, type=Path)
    p.add_argument('--font', required=True, type=Path)
    a = p.parse_args()
    out = a.archive/'plots'
    out.mkdir(exist_ok=False)
    def read(name): return json.loads((a.archive/name).read_text())
    def lines(name): return [json.loads(x) for x in (a.archive/name).read_text().splitlines()]
    def stamp(text): return datetime.fromisoformat(text)
    host = lines('app/samples.jsonl')
    external = lines('external/samples.jsonl')
    app = lines('app/access.jsonl')
    loki = lines('plg/loki-access.jsonl')
    aids = Counter(x['request_id'] for x in app if x.get('request_id'))
    lids = Counter(x['data']['request_id'] for x in loki if x.get('data', {}).get('request_id'))
    names = ['reuse10a', 'fresh10a', 'fresh10b', 'reuse10b', 'reuse100', 'reuse450', 'tls10', 'fresh20']
    stages = {}
    raw = {}
    for name in names:
        path = a.archive/'generator'/(name+'-result.json')
        if not path.exists(): continue
        r = json.loads(path.read_text())
        raw[name] = r
        h = [x for x in host if stamp(r['startedAt']) <= stamp(x['utc']) <= stamp(r['endedAt'])]
        worst = [max((p['cpuPercentOneCore'] for p in x.get('processes', [])
                      if p.get('name') == 'nginx' and p.get('cpuPercentOneCore') is not None), default=None) for x in h]
        missing_worker_samples = sum(x is None for x in worst)
        worst = [x for x in worst if x is not None]
        stage = {k: v for k, v in r.items() if k not in ('rows', 'samples')}
        stage.update(hostSamples=len(h), missingWorkerSamples=missing_worker_samples, nginxMaxProcessCpuPercent=load.duration_summary(worst),
                     hostCpuPercent=load.duration_summary([x['cpuPercent'] for x in h if x.get('cpuPercent') is not None]),
                     active=load.duration_summary([x['active'] for x in h if x.get('active') is not None]))
        e = [x for x in external if stamp(r['startedAt']) <= stamp(x['utc']) <= stamp(r['endedAt'])]
        stage['externalSamples'] = len(e)
        stage['externalFailures'] = sum(not x.get('ok', False) for x in e)
        stage['externalTlsMs'] = load.duration_summary([(x['timings']['tls']-x['timings']['tcp'])*1000
                                                       for x in e if x.get('exitCode') == 0])
        stages[name] = stage
    analysis = {'stages': stages, 'external': read('external/summary.json'),
                'host': read('app/result.json'), 'ena': read('app/ena-summary.json'),
                'app': read('app/index.json'),
                'normal': json.loads(next((a.archive/'app').glob('normal-*-summary.json')).read_text()),
                'requestIds': {'app': sum(aids.values()), 'loki': sum(lids.values()),
                               'missing': sum((aids-lids).values()), 'extra': sum((lids-aids).values())},
                'limits': ['TLS1.2 / HTTP1.1 / no session resumption; IPv4 generator, not LTE.',
                           'New connections <=20/s with existing 20/s burst40 SYN protection unchanged.',
                           'Negative GET401/429 are not authenticated-user successes.',
                           'TLS timing includes network, scheduling and server queue, not pure crypto CPU.',
                           'Two paired runs are a small sample, not a population performance guarantee.']}
    (out/'analysis.json').write_text(json.dumps(analysis, ensure_ascii=False, indent=2))
    os.environ.setdefault('MPLCONFIGDIR', tempfile.mkdtemp(prefix='tls440-plot-'))
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib import font_manager, dates
    font_manager.fontManager.addfont(str(a.font))
    plt.rcParams.update({'font.family': font_manager.FontProperties(fname=a.font).get_name(),
                         'axes.unicode_minus': False, 'font.size': 11,
                         'axes.spines.top': False, 'axes.spines.right': False})
    labels = {'reuse10a': '재사용10\n①', 'fresh10a': '신규TLS10\n①', 'fresh10b': '신규TLS10\n②',
              'reuse10b': '재사용10\n②', 'reuse100': '재사용100', 'reuse450': '재사용450',
              'tls10': 'TLS만10', 'fresh20': '신규TLS20'}
    names = [n for n in names if n in stages]
    positions = list(range(len(names)))
    fig, axes = plt.subplots(2, 1, figsize=(12.8, 8))
    fig.subplots_adjust(left=.09, right=.97, top=.86, bottom=.17, hspace=.55)
    fig.suptitle('HTTP 요청 수와 실제 TLS 핸드셰이크 수를 분리했다', x=.09, y=.96, ha='left', fontsize=20, weight='bold')
    for offset, key, label, color in [(-.19, 'statuses', 'HTTP 응답', '#2256c5'), (.19, 'tlsCompleted', '완료한 TLS', '#d98223')]:
        counts = [sum(stages[n][key].values()) if key == 'statuses' else stages[n][key] for n in names]
        bars = axes[0].bar([i+offset for i in positions], counts, width=.36, label=label, color=color)
        axes[0].bar_label(bars, fontsize=9, padding=3)
    axes[0].set_ylabel('실제 건수 · 단계당 최대 30초')
    axes[0].legend(frameon=False, ncol=2)
    values = [stages[n]['nginxMaxProcessCpuPercent']['p95'] if stages[n]['nginxMaxProcessCpuPercent'] else 0 for n in names]
    bars = axes[1].bar(positions, values, color='#249487')
    axes[1].bar_label(bars, fmt='%.1f%%', fontsize=10, padding=3)
    axes[1].set_ylabel('가장 바쁜 Nginx 프로세스 CPU p95\n100% = 논리 CPU 1개')
    axes[1].set_ylim(0, max(10, max(values, default=0)*1.3))
    for ax in axes:
        ax.set_xticks(positions, [labels[n] for n in names]);ax.grid(axis='y', alpha=.2);ax.set_axisbelow(True)
    failed = [n for n in names if stages[n]['stopped'] or stages[n]['schedulerDropped']]
    note = 'TLS 1.2 · HTTP/1.1 · 세션 재개 0 · 기존 방어 유지 · 401/429는 시험 요청의 예상 거절'
    if failed: note += '\n미완료/누락 단계: '+', '.join(failed)+' · 목표 처리량 달성으로 해석하지 않음'
    fig.text(.09, .035, note, fontsize=10, color='#526078')
    fig.savefig(out/'01-http-tls-comparison-ko.png', dpi=170);plt.close(fig)

    kst = ZoneInfo('Asia/Seoul')
    at = lambda t: stamp(t).astimezone(kst)
    fig, axes = plt.subplots(2, 1, figsize=(12.8, 7.5), sharex=True)
    fig.subplots_adjust(left=.09, right=.97, top=.85, bottom=.16, hspace=.4)
    fig.suptitle('독립 외부 접속에서 TCP와 TLS 구간을 따로 확인했다', x=.09, y=.96, ha='left', fontsize=20, weight='bold')
    valid = [x for x in external if x.get('exitCode') == 0]
    for before, after, label, color in [('dns', 'tcp', 'TCP 연결 구간', '#2256c5'), ('tcp', 'tls', 'TLS 핸드셰이크 구간', '#d98223')]:
        axes[0].plot([at(x['utc']) for x in valid], [(x['timings'][after]-x['timings'][before])*1000 for x in valid],
                     label=label, lw=1.2, color=color)
    axes[0].set_ylabel('독립 Mac 접속 시간 (ms)');axes[0].legend(frameon=False, ncol=2)
    for name in names:
        r = raw[name]
        for ax in axes:
            ax.axvspan(at(r['startedAt']), at(r['endedAt']), alpha=.07, color='#2256c5')
        axes[0].text(at(r['startedAt']), 1.02, labels[name].replace('\n', ''), fontsize=8,
                     transform=axes[0].get_xaxis_transform(), rotation=25)
    axes[1].plot([at(x['utc']) for x in host], [x.get('active') for x in host], color='#249487', label='Nginx 활성 연결')
    axes[1].set_ylabel('활성 연결 수');axes[1].legend(frameon=False)
    for ax in axes:
        ax.set_ylim(bottom=0);ax.grid(axis='y', alpha=.2)
        ax.xaxis.set_major_formatter(dates.DateFormatter('%H:%M', tz=kst))
    axes[1].set_xlabel('2026-10-27 한국 시간 · 음영은 실제 발생기 실행 구간')
    fig.text(.09, .03, 'Mac probe는 부하 발생기와 독립 출처 · LTE·화면 렌더링·HTTP/2의 증거와 구분', fontsize=10, color='#526078')
    fig.savefig(out/'02-external-tcp-tls-ko.png', dpi=170);plt.close(fig)
    (out/'source-hashes.json').write_text(json.dumps({str(f.relative_to(a.archive)): hashlib.sha256(f.read_bytes()).hexdigest()
        for f in [a.archive/'app/samples.jsonl', a.archive/'external/samples.jsonl',
                  *sorted((a.archive/'generator').glob('*-result.json'))]}, indent=2))
    print(json.dumps({'requestIds': analysis['requestIds'], 'stages': {n: {'errors': s['errors'], 'http': s['statuses'],
          'tls': s['tlsCompleted'], 'nginxCpu': s['nginxMaxProcessCpuPercent']} for n, s in stages.items()}}, ensure_ascii=False))


if __name__ == '__main__': main()
