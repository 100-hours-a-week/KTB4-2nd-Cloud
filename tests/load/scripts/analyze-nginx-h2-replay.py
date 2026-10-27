#!/usr/bin/env python3
"""Offline #442 client/DB/Nginx reconciliation and Korean evidence figures."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
from zoneinfo import ZoneInfo

spec = importlib.util.spec_from_file_location('tls440', Path(__file__).with_name('nginx-http-tls-load.py'))
metric = importlib.util.module_from_spec(spec)
spec.loader.exec_module(metric)


def lines(path):
    with path.open() as stream:
        return [json.loads(x) for x in stream if x.strip()]


def reconcile(events, db, access):
    starts = [x for x in events if x.get('event') == 'started']
    responses = [x for x in events if x.get('event') == 'response']
    started_keys = Counter((x['userId'], x['tripName']) for x in starts)
    final_keys = Counter((x['userId'], x['tripName']) for x in responses)
    stored = Counter((x['userId'], x['tripName']) for x in db)
    stored_ids = {(x['tripId'], x['userId'], x['tripName']) for x in db}
    committed = [x for x in responses if x['status'] == 201]
    confirmed = {(x['userId'], x['tripName']) for x in committed}
    ids = Counter(x['request_id'] for x in access if x.get('request_id'))
    return {'started': len(starts), 'responses': len(responses),
            'startedWithoutResponse': sum((started_keys-final_keys).values()),
            'duplicateStartKeys': sum(v-1 for v in started_keys.values()),
            'confirmed201': len(committed),
            'confirmedMissingDb': sum((x['tripId'], x['userId'], x['tripName']) not in stored_ids for x in committed),
            'dbRowsWithout201': sum(stored[k] for k in started_keys if k not in confirmed),
            'duplicateDbRows': sum(max(0, stored[k]-1) for k in started_keys),
            'responseIdsMissingOrDuplicateNginx': sum(ids[x['requestId']] != 1 for x in responses if x.get('requestId'))}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--archive', type=Path, required=True)
    p.add_argument('--font', type=Path, required=True)
    a = p.parse_args();out = a.archive/'analysis';out.mkdir(exist_ok=False)
    read = lambda n: json.loads((a.archive/n).read_text())
    access = lines(a.archive/'app/access.jsonl')
    loki = lines(a.archive/'plg/loki-access.jsonl')
    host = lines(a.archive/'app/samples.jsonl')
    external = lines(a.archive/'external/samples.jsonl')
    audit = read('app/journey-audit.json')
    aid = Counter(x['request_id'] for x in access if x.get('request_id'))
    lid = Counter(x['data']['request_id'] for x in loki if x.get('data', {}).get('request_id'))
    access_by_id = {x['request_id']: x for x in access if x.get('request_id')}
    stages = {};sequences = {}
    names = ['smoke1', 'small16', 'medium64', 'instructor141-scheduler-prep-stop', 'instructor141', 'instructor128']
    for name in names:
        folder = a.archive/'generator'
        path = folder/(name+'-events.jsonl')
        if not path.exists(): continue
        events = lines(path);responses = [x for x in events if x.get('event') == 'response']
        started = [x for x in events if x.get('event') == 'started']
        setup = next(x for x in events if x.get('event') == 'run_started')
        plan = read('generator/'+name+'-plan.json')
        result_path = folder/(name+'-result.json')
        result = json.loads(result_path.read_text()) if result_path.exists() else {'exitCode': None, 'incompleteResult': True}
        matched = [access_by_id[x['requestId']] for x in responses if x.get('requestId') in access_by_id]
        counts = Counter((str(x.get('worker_pid')), str(x['connection'])) for x in matched)
        begin = setup['startMs']+setup['warmupSeconds']*1000
        end = begin+setup['seconds']*1000
        timed = [x for x in host if begin <= datetime.fromisoformat(x['utc']).timestamp()*1000 < end]
        cpu = [max((p['cpuPercentOneCore'] for p in x.get('processes', [])
                    if p.get('name') == 'nginx' and p.get('cpuPercentOneCore') is not None), default=None) for x in timed]
        replay = [x for x in started if x['phase'] == 'replay']
        bins = Counter(int((x['atMs']-begin)//1000) for x in replay)
        series = [bins[i] for i in range(setup['seconds'])]
        sequences[name] = series
        stage = {'plan': plan, 'result': result, 'setup': setup,
                 'replayStartUtc': datetime.fromtimestamp(begin/1000, timezone.utc).isoformat(),
                 'replayExpected': setup['expectedReplayRequests'], 'replayActuallyStarted': len(replay),
                 'replayNotStarted': setup['expectedReplayRequests']-len(replay),
                 'clientStatuses': dict(Counter(str(x['status']) for x in responses)),
                 'clientProtocols': dict(Counter(x['protocol'] for x in responses)),
                 'warmupFailed': [x for x in responses if x['phase'] == 'warmup' and x['status'] not in [201, 429]],
                 'allTransportFailures': [x for x in responses if x['status'] == 0],
                 'nginxConnections': len(counts), 'nginxReusedConnections': sum(v > 1 for v in counts.values()),
                 'maxRequestsPerNginxConnection': max(counts.values(), default=0),
                 'nginxProtocols': dict(Counter(x.get('http_protocol') for x in matched)),
                 'httpStartPerSecond': metric.duration_summary(series),
                 'reconciliation': reconcile(events, audit['trips'], access),
                 'hostSamples': len(timed), 'missingWorkerSamples': sum(v is None for v in cpu),
                 'nginxCpuPercentOneCore': metric.duration_summary([v for v in cpu if v is not None]),
                 'acceptedOrRejectedPostLatencyMs': metric.duration_summary([x['endedAtMs']-x['atMs'] for x in responses if x['status'] in [201, 429]])}
        # Warmup 0 responses are not evidence that 133875 replay operations were sent.
        stages[name] = stage
    normal = json.loads(next((a.archive/'app').glob('normal-*-summary.json')).read_text())
    nft = [x['tcpGuardCounters'] for x in host if 'tcpGuardCounters' in x]
    counters = lambda r: {x['name']: x['packets'] for x in r}
    first, last = counters(nft[0]), counters(nft[-1])
    summary = {'stages': stages, 'requestIds': {'app': sum(aid.values()), 'loki': sum(lid.values()),
                'missing': sum((aid-lid).values()), 'extra': sum((lid-aid).values())},
               'normal': normal, 'external': read('external/summary.json'),
               'host': read('app/result.json'), 'app': read('app/index.json'), 'ena': read('app/ena-summary.json'),
               'nftDelta': {k: last[k]-first[k] for k in first},
               'audit': {'rows': len(audit['trips']), 'regions': sum(x['regionRows'] for x in audit['trips']),
                         'attachments': sum(x['attachmentRows'] for x in audit['trips'])},
               'limitations': ['Profile is original HTTP completions used as new request starts, not original offered load.',
                 '141 clients could fail before HTTP; 128 is a distinct controlled variant.',
                 'Payload/auth/one in-flight stream and 20/s connection ramp are explicit chosen conditions.',
                 'Independent Mac HTTP2 is not LTE or full browser/OAuth coverage.']}
    (out/'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    os.environ.setdefault('MPLCONFIGDIR', tempfile.mkdtemp(prefix='h2442-plot-'))
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib import font_manager, dates
    font_manager.fontManager.addfont(str(a.font))
    plt.rcParams.update({'font.family': font_manager.FontProperties(fname=a.font).get_name(),
                         'font.size': 11, 'axes.unicode_minus': False,
                         'axes.spines.right': False, 'axes.spines.top': False})
    profile = read('generator/profile.json')['httpCompletionProfile']
    fig,axes = plt.subplots(2,1,figsize=(12.8,7.5),sharex=True)
    fig.subplots_adjust(left=.085,right=.97,top=.85,bottom=.16,hspace=.45)
    fig.suptitle('강사 부하의 초별 분포와 실제 송신량을 대조했다',x=.085,y=.96,ha='left',fontsize=20,weight='bold')
    original = [x['completedHttpRequests'] for x in profile]
    for ax,name,label,color in zip(axes,['instructor141','instructor128'],['141 클라이언트 시도','128 클라이언트 분리 시험'],['#d98223','#2256c5']):
        ax.plot(range(299),original,label='강사 서버 완료 기록',color='#8a95a5',lw=1.3)
        if name in stages:
            ax.plot(range(299),sequences[name],label=label+' · 실제 요청 시작',color=color,lw=1.2)
            s=stages[name]
            ax.set_title(f"{label}: {s['replayActuallyStarted']:,} / 목표 133,875건 · HTTP 전송 실패 {len(s['allTransportFailures'])}건",loc='left',fontsize=12)
        ax.set_ylabel('1초당 요청 수');ax.set_ylim(bottom=0);ax.grid(axis='y',alpha=.2);ax.legend(frameon=False,loc='lower right')
    axes[-1].set_xlabel('본부하 시작 후 경과 시간 (초)')
    fig.text(.085,.03,'원본은 서버 완료 시각, 이번 값은 클라이언트 시작 시각 · 둘의 의미와 연결 수 차이를 구분',fontsize=10,color='#526078')
    fig.savefig(out/'01-http2-profile-comparison-ko.png',dpi=170);plt.close(fig)
    kst=ZoneInfo('Asia/Seoul');at=lambda t:datetime.fromisoformat(t).astimezone(kst)
    fig,axes=plt.subplots(2,1,figsize=(12.8,7.5),sharex=True)
    fig.subplots_adjust(left=.085,right=.97,top=.85,bottom=.16,hspace=.4)
    fig.suptitle('부하 연결 점유와 독립 HTTP/2 접속을 함께 확인했다',x=.085,y=.96,ha='left',fontsize=20,weight='bold')
    axes[0].plot([at(x['utc']) for x in host],[x.get('active') for x in host],label='Nginx 활성 연결',color='#2256c5')
    axes[0].set_ylabel('활성 연결');axes[0].legend(frameon=False)
    valid=[x for x in external if x.get('timings')]
    axes[1].plot([at(x['utc']) for x in valid],[x['timings']['total']*1000 for x in valid],'.-',ms=2,lw=.8,color='#249487',label='독립 Mac HTTP/2 전체 응답')
    axes[1].set_ylabel('전체 응답 시간 (ms)');axes[1].legend(frameon=False)
    for name in ['instructor141','instructor128']:
        if name not in stages:continue
        start=at(stages[name]['replayStartUtc']);end=start.timestamp()+299
        for ax in axes:ax.axvspan(start,datetime.fromtimestamp(end,kst),color='#2256c5',alpha=.06)
        axes[0].text(start,1.02,name,transform=axes[0].get_xaxis_transform(),fontsize=9)
    for ax in axes:
        ax.grid(axis='y',alpha=.2);ax.set_ylim(bottom=0)
        ax.xaxis.set_major_formatter(dates.DateFormatter('%H:%M',tz=kst))
    axes[-1].set_xlabel('2026-10-27 한국 시간')
    fig.text(.085,.03,'기존 방어 유지 · 외부 probe는 새 TCP/TLS와 HTTP/2를 사용 · LTE·화면 렌더링 보장과 구분',fontsize=10,color='#526078')
    fig.savefig(out/'02-http2-external-and-connections-ko.png',dpi=170);plt.close(fig)
    (out/'source-hashes.json').write_text(json.dumps({str(f.relative_to(a.archive)):hashlib.sha256(f.read_bytes()).hexdigest()
       for f in [a.archive/'app/access.jsonl',a.archive/'app/samples.jsonl',a.archive/'external/samples.jsonl',
                 *sorted((a.archive/'generator').glob('*-events.jsonl'))]},indent=2))
    print(json.dumps({'requestIds':summary['requestIds'],'audit':summary['audit'],'nft':summary['nftDelta'],
       'stages':{n:{k:s[k] for k in ['replayActuallyStarted','replayNotStarted','clientStatuses','nginxConnections','maxRequestsPerNginxConnection','reconciliation']} for n,s in stages.items()}},ensure_ascii=False))


if __name__ == '__main__': main()
