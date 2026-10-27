#!/usr/bin/env python3
"""Offline #447 workload, rendered-screen and independent transport evidence comparison."""
import argparse
from collections import Counter
from datetime import datetime
import importlib.util
import json
import os
from pathlib import Path
import tempfile
from zoneinfo import ZoneInfo

spec=importlib.util.spec_from_file_location('h2442',Path(__file__).with_name('analyze-nginx-h2-replay.py'))
prior=importlib.util.module_from_spec(spec);spec.loader.exec_module(prior)


def analyze(root):
    read=lambda n:json.loads((root/n).read_text())
    rows=lambda n:[json.loads(x) for x in (root/n).read_text().splitlines() if x.strip()]
    access=rows('app/access.jsonl');host=rows('app/samples.jsonl')
    aid=Counter(x['request_id'] for x in access)
    lid=Counter(x['data']['request_id'] for x in rows('plg/loki-access.jsonl'))
    by_id={x['request_id']:x for x in access}
    history=rows('browser-guard/history.jsonl');external=rows('external/samples.jsonl')
    browser=[];browser_sources=set();browser_ids=[]
    for mark in history:
        if not (root/'browser'/(mark['round']+'.json')).is_file():
            browser.append({**mark,'captureMissing':True,'applicationRequests':None,
                'responseIds':[],'missingServerIds':[],'documentConnections':[]})
            continue
        report=read('browser/'+mark['round']+'.json')
        ids=[x['requestId'] for x in report['applicationRequests'] if x.get('requestId')]
        browser_ids.extend(ids)
        matched=[by_id[x] for x in ids if x in by_id]
        browser_sources.update(x['sourceId'] for x in matched)
        docs=[x for x in report['applicationRequests'] if x['resourceType']=='Document']
        browser.append({**mark,'allRequests':len(report['requests']),
            'applicationRequests':len(report['applicationRequests']),'pending':len(report['pending']),
            'unmatchedEvents':report['unmatchedEvents'],
            'documentConnections':[{'reused':x.get('connectionReused'),'protocol':x.get('protocol'),
                'tls':x.get('tls'),'timing':x.get('timing')} for x in docs],
            'responseIds':ids,'missingServerIds':[x for x in ids if x not in by_id]})
    stages={};events=[];load_sources=set()
    audit=read('app/journey-audit.json')
    for name in ['smoke1','small16','medium64','instructor128']:
        filename=root/'generator'/(name+'-events.jsonl')
        if not filename.exists():continue
        stream=[json.loads(x) for x in filename.read_text().splitlines() if x.strip()];events.extend(stream)
        starts=[x for x in stream if x.get('event')=='started']
        responses=[x for x in stream if x.get('event')=='response']
        setup=next(x for x in stream if x.get('event')=='run_started')
        matched=[by_id[x['requestId']] for x in responses if x.get('requestId') in by_id]
        load_sources.update(x['sourceId'] for x in matched)
        keys=Counter((x['worker_pid'],x['connection']) for x in matched)
        begin=min((x['atMs'] for x in starts),default=setup['startMs'])/1000
        end=max((x['endedAtMs'] for x in responses),default=begin*1000)/1000
        marks=[x for x in browser if begin<=datetime.fromisoformat(x['updatedAt']).timestamp()<=end]
        stages[name]={'startEpoch':begin,'endEpoch':end,'includesWarmup':True,
            'replayExpected':setup['expectedReplayRequests'],
            'replayStarted':sum(x['phase']=='replay' for x in starts),
            'statuses':dict(Counter(str(x['status']) for x in responses)),
            'transportFailures':sum(x['status']==0 for x in responses),
            'nginxConnections':len(keys),'maxRequestsPerConnection':max(keys.values(),default=0),
            'reconciliation':prior.reconcile(stream,audit['trips'],access),
            'browserViewsInActualWindow':dict(Counter(x['view'] for x in marks)),
            'browserFailedInActualWindow':sum(not x['result']['ok'] for x in marks)}
    sample_counters=lambda x:{r['name']:r['packets'] for r in x['tcpGuardCounters']}
    nft=[x for x in host if 'tcpGuardCounters' in x];first,last=sample_counters(nft[0]),sample_counters(nft[-1])
    normal=read('app/'+next((root/'app').glob('normal-*-summary.json')).name)
    summary={'stages':stages,'browser':browser,'browserSamples':len(browser),
        'browserFailures':sum(not x['result']['ok'] for x in browser),
        'browserServerIds':{'observed':len(browser_ids),'missing':sum(x not in by_id for x in browser_ids)},
        'sourceSeparation':{'browserSourceCount':len(browser_sources),'loadSourceCount':len(load_sources),
            'disjoint':bool(browser_sources and load_sources) and not bool(browser_sources & load_sources)},
        'appLoki':{'app':sum(aid.values()),'loki':sum(lid.values()),'missing':sum((aid-lid).values()),'extra':sum((lid-aid).values())},
        'nftDelta':{k:last[k]-first[k] for k in first},'normal':normal,
        'host':read('app/result.json'),'app':read('app/index.json'),'ena':read('app/ena-summary.json'),
        'tcp':read('app/tcp445-summary.json'),'external':read('external/summary.json'),
        'audit':{'trips':len(audit['trips']),'regions':sum(x['regionRows'] for x in audit['trips']),
            'attachments':sum(x['attachmentRows'] for x in audit['trips'])},
        'limits':['Mac is not LTE; normal direct URL navigation, not full OAuth/upload/AI or every UI interaction.',
            'Browser checkpoint time includes capture overhead; not an exact render latency.',
            'Pending fonts and canceled prefetch remain in raw evidence; not converted into completed requests.',
            'Workload 429/transport rejection and independent normal user failure are different outcomes.',
            'TCP source filter captures this observer source, not every network flow; no TLS decryption.']}
    return summary,host,external,events


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--archive',type=Path,required=True);p.add_argument('--font',type=Path,required=True)
    a=p.parse_args();out=a.archive/'analysis';out.mkdir(exist_ok=False)
    summary,host,external,events=analyze(a.archive)
    (out/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2))
    os.environ.setdefault('MPLCONFIGDIR',tempfile.mkdtemp(prefix='nginx447-mpl-'))
    import matplotlib
    matplotlib.use('Agg')
    from matplotlib import pyplot as plt,dates,font_manager
    font_manager.fontManager.addfont(str(a.font))
    plt.rcParams.update({'font.family':font_manager.FontProperties(fname=a.font).get_name(),
        'font.size':11,'axes.unicode_minus':False,'axes.spines.top':False,'axes.spines.right':False})
    kst=ZoneInfo('Asia/Seoul');at=lambda t:datetime.fromisoformat(t).astimezone(kst)
    epoch=lambda s:datetime.fromtimestamp(s,kst)
    fig,axes=plt.subplots(3,1,figsize=(13,9),sharex=True)
    fig.subplots_adjust(left=.1,right=.97,top=.87,bottom=.12,hspace=.5)
    fig.suptitle('자체 부하 중 독립 브라우저의 실제 화면을 확인했다',x=.1,y=.97,ha='left',fontsize=19,weight='bold')
    bins=Counter(int(x['atMs']/1000) for x in events if x.get('event')=='started')
    seconds=range(min(bins),max(bins)+1)
    axes[0].plot([epoch(t) for t in seconds],[bins[t] for t in seconds],color='#3663be',lw=1,label='발생기 실제 POST 시작')
    axes[0].set_ylabel('초당 요청 수');axes[0].legend(frameon=False,loc='upper left')
    labels={'login':0,'home':1,'list':2,'detail':3}
    for r in summary['browser']:
        axes[1].scatter(at(r['updatedAt']),labels[r['view']],color='#23896e' if r['result']['ok'] else '#c94242',
            marker='o' if r['result']['ok'] else 'x',s=24)
    axes[1].set_yticks([0,1,2,3],['로그인 화면','홈 지도','여행 목록','여행 상세'])
    axes[1].set_ylim(-.6,3.6);axes[1].set_title('점 = 기대 화면 확인 · 실패는 빨간 ×',loc='left',fontsize=11)
    valid=[x for x in external if x.get('timings')]
    axes[2].plot([at(x['utc']) for x in valid],[x['timings']['total']*1000 for x in valid],color='#23896e',lw=.9,label='별도 새 연결 HTTP/2 전체 시간')
    axes[2].set_ylabel('응답 시간 (ms)');axes[2].legend(frameon=False,loc='upper left')
    for ax in axes:
        for name,s in summary['stages'].items():
            ax.axvspan(epoch(s['startEpoch']),epoch(s['endEpoch']),color='#3663be',alpha=.06)
        ax.grid(axis='y',alpha=.18);ax.xaxis.set_major_formatter(dates.DateFormatter('%H:%M:%S',tz=kst))
    for i,(name,s) in enumerate(summary['stages'].items()):
        axes[0].text(epoch(s['startEpoch']),1.02+(i%2)*.1,{'smoke1':'사전 확인','small16':'16클라이언트','medium64':'64클라이언트','instructor128':'128클라이언트'}[name],transform=axes[0].get_xaxis_transform(),fontsize=9)
    axes[-1].set_xlabel('2026-10-27 한국 시간 · 음영은 실제 클라이언트 요청 구간(준비 포함)')
    fig.text(.1,.025,'기존 방어 유지 · 외부 Mac은 발생기와 다른 출처 · 실제 LTE·OAuth·업로드 완료는 미검증',fontsize=10,color='#536176')
    fig.savefig(out/'01-load-browser-and-external-ko.png',dpi=170);plt.close(fig)
    print(json.dumps({k:summary[k] for k in ['browserSamples','browserFailures','browserServerIds','sourceSeparation','appLoki','nftDelta','audit']},ensure_ascii=False))


if __name__=='__main__':main()
