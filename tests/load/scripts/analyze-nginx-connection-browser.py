#!/usr/bin/env python3
"""Offline connection-load/browser comparison; missing workload data is not a success."""
import argparse
from collections import Counter
from datetime import datetime
import json
import os
from pathlib import Path
import tempfile
from zoneinfo import ZoneInfo


def epoch(value):
    return datetime.fromisoformat(value.strip()).timestamp()


def analyze(root):
    read=lambda name:json.loads((root/name).read_text())
    rows=lambda name:[json.loads(x) for x in (root/name).read_text().splitlines() if x.strip()]
    access=rows('app/access.jsonl');host=rows('app/samples.jsonl')
    app_ids=Counter(x['request_id'] for x in access)
    loki_ids=Counter(x['data']['request_id'] for x in rows('plg/loki-access.jsonl'))
    by_id={x['request_id']:x for x in access};browser=[];browser_sources=set()
    for mark in rows('browser-guard/history.jsonl'):
        path=root/'browser'/(mark['round']+'.json')
        if not path.exists():
            browser.append({**mark,'captureMissing':True});continue
        r=json.loads(path.read_text());requests=r['applicationRequests']
        ids=[x['requestId'] for x in requests if x.get('requestId')]
        browser_sources.update(by_id[x]['sourceId'] for x in ids if x in by_id)
        browser.append({**mark,'captureMissing':False,'applicationRequests':len(requests),
            'responseIds':ids,'missingServerIds':[x for x in ids if x not in by_id],
            'unmatchedEvents':r['unmatchedEvents'],'pending':len(r['pending']),
            'documentConnections':[{'reused':x.get('connectionReused'),'protocol':x.get('protocol')}
                for x in requests if x['resourceType']=='Document'],
            'firstRequestEpoch':min((epoch(x['startedAt']) for x in requests),default=None),
            'capturedEpoch':epoch(r['capturedAt'])})
    stages=[];sockets=[]
    for stage in read('trials/stages.json'):
        name=stage['stage'];g=root/'generator';item={**stage,'http':None,'tcp':None,'workloadWindows':[]}
        summary=g/(name+'-http-summary.json')
        if summary.exists():
            metrics=json.loads(summary.read_text())['metrics']
            item['http']={k:metrics[k] for k in ['http_reqs','http_req_failed','dropped_iterations','checks'] if k in metrics}
            start=g/(name+'-http-start.txt');end=g/(name+'-http-end.txt')
            if start.exists() and end.exists():
                item['workloadWindows'].append({'kind':'http-process','start':epoch(start.read_text()),'end':epoch(end.read_text())})
        tcp=g/(name+'-tcp.json')
        if tcp.exists() and tcp.stat().st_size:
            data=json.loads(tcp.read_text())
            item['tcp']={k:data[k] for k in ['attempted','opened','peakGeneratorSockets','httpStatuses','errors','stopped','recoveryProbes']}
            sockets.extend({**s,'stage':name} for s in data['samples'])
            if data['rows']:
                item['workloadWindows'].append({'kind':'tcp-actors','start':min(epoch(x['startedAt']) for x in data['rows']),
                    'end':max(epoch(x['endedAt']) for x in data['rows'])})
        # Require the complete measured browser round to fit within a real workload window.
        marks=[r for r in browser if not r['captureMissing'] and r['firstRequestEpoch'] is not None and
            any(w['start']<=r['firstRequestEpoch'] and r['capturedEpoch']<=w['end'] for w in item['workloadWindows'])]
        item['completeBrowserRoundsInWorkloadWindow']=dict(Counter(r['view'] for r in marks))
        item['browserFailuresInWorkloadWindow']=sum(not r['result']['ok'] for r in marks)
        item['perWindowBrowserRounds']=[{'kind':w['kind'],'views':dict(Counter(r['view'] for r in marks
            if w['start']<=r['firstRequestEpoch'] and r['capturedEpoch']<=w['end']))} for w in item['workloadWindows']]
        if len(item['workloadWindows'])==2:
            a,b=item['workloadWindows'];left=max(a['start'],b['start']);right=min(a['end'],b['end'])
            item['mixedOverlapSeconds']=max(0,right-left)
            item['mixedConcurrentCompleteBrowserRounds']=dict(Counter(r['view'] for r in marks
                if left<=r['firstRequestEpoch'] and r['capturedEpoch']<=right))
        item['workloadEvidencePresent']=bool(item['workloadWindows'])
        stages.append(item)
    nft=[{x['name']:x['packets'] for x in r['tcpGuardCounters']} for r in host if 'tcpGuardCounters' in r]
    normal=read('app/'+next((root/'app').glob('normal-*-summary.json')).name)
    marker=read('generator-source-marker.json')
    matched=by_id.get(marker['requestId'])
    load_sources={matched['sourceId']} if matched else set()
    for stage in stages:
        selected=[r for r in access if r['sourceId'] in load_sources and
            r.get('normalized_route') in ['/api/trips','/api/trips/map'] and
            any(w['start']<=float(r['msec'])<=w['end'] for w in stage['workloadWindows'])]
        stage['generatorSourceServerStatuses']=dict(Counter(str(r['status']) for r in selected))
    result={'stages':stages,'browser':browser,'browserSamples':len(browser),
        'browserFailures':sum(not r['result']['ok'] for r in browser),
        'missingBrowserCaptures':sum(r['captureMissing'] for r in browser),
        'browserServerIds':{'observed':sum(len(r.get('responseIds',[])) for r in browser),
            'missing':sum(len(r.get('missingServerIds',[])) for r in browser)},
        'sourceSeparation':{'browserSourceCount':len(browser_sources),'loadSourceCount':len(load_sources),
            'generatorMarkerMatched':bool(matched),
            'disjoint':bool(browser_sources and load_sources) and not bool(browser_sources & load_sources)},
        'appLoki':{'app':sum(app_ids.values()),'loki':sum(loki_ids.values()),
            'missing':sum((app_ids-loki_ids).values()),'extra':sum((loki_ids-app_ids).values())},
        'serverStatuses':dict(Counter(str(r['status']) for r in access)),
        'hostMax':{'nginxActive':max((r['active'] for r in host if isinstance(r.get('active'),int)),default=None),
            'cpuPercent':max((r['cpuPercent'] for r in host if isinstance(r.get('cpuPercent'),(int,float))),default=None),
            'nginxFdTotal':max((r['nginxFdTotal'] for r in host if isinstance(r.get('nginxFdTotal'),int)),default=None)},
        'nftDelta':{k:nft[-1][k]-nft[0][k] for k in nft[0]} if nft else None,
        'host':read('app/result.json'),'ena':read('app/ena-summary.json'),
        'tcpCapture':read('app/tcp445-summary.json'),'normal':normal,'external':read('external/summary.json'),
        'limits':['Mac is not LTE; direct URL rendering excludes OAuth, upload, AI and every UI interaction.',
            'HTTP process windows include k6 startup and warmup; TCP actor windows exclude final recovery probes.',
            'Requested connections and rates are not achieved throughput; preserve policy drops and workload failures.',
            'A healthy browser does not locate the original LTE failure.']}
    return result,host,sockets,rows('external/samples.jsonl')


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--archive',type=Path,required=True);p.add_argument('--font',type=Path,required=True)
    a=p.parse_args();out=a.archive/'analysis';out.mkdir(exist_ok=False)
    summary,host,sockets,external=analyze(a.archive)
    (out/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
    os.environ.setdefault('MPLCONFIGDIR',tempfile.mkdtemp(prefix='nginx449-mpl-'))
    import matplotlib
    matplotlib.use('Agg')
    from matplotlib import pyplot as plt,dates,font_manager
    font_manager.fontManager.addfont(str(a.font))
    plt.rcParams.update({'font.family':font_manager.FontProperties(fname=a.font).get_name(),'font.size':11,
        'axes.unicode_minus':False,'axes.spines.top':False,'axes.spines.right':False})
    kst=ZoneInfo('Asia/Seoul');at=lambda t:datetime.fromisoformat(t).astimezone(kst)
    stamp=lambda t:datetime.fromtimestamp(t,kst)
    fig,axes=plt.subplots(3,1,figsize=(13,9),sharex=True)
    fig.subplots_adjust(left=.11,right=.97,top=.87,bottom=.12,hspace=.48)
    fig.suptitle('연결 유지·교체 부하와 독립 브라우저 접속을 함께 확인했다',x=.11,y=.97,ha='left',fontsize=18,weight='bold')
    mixed=next((s for s in summary['stages'] if s['stage']=='mixed450'),None)
    if mixed and mixed['status']!='Success' and 'mixedOverlapSeconds' in mixed:
        fig.text(.11,.925,f"혼합 단계: HTTP는 약 {mixed['mixedOverlapSeconds']:.1f}초 중첩 후 종료 · 이후 연결 교체 부하는 별도 확인",fontsize=11,color='#8b5120')
    axes[0].plot([at(x['utc']) for x in host],[x.get('active') for x in host],label='Nginx Active',color='#3663be')
    axes[0].plot([at(x['utc']) for x in sockets],[x['activeGeneratorSockets'] for x in sockets],'.',ms=2,label='발생기 TLS 소켓',color='#db9137')
    axes[0].set_ylabel('연결 수');axes[0].legend(frameon=False,ncol=2,loc='upper left')
    positions={'login':0,'home':1,'list':2,'detail':3}
    for r in summary['browser']:
        axes[1].scatter(at(r['updatedAt']),positions[r['view']],s=20,
            color='#23896e' if r['result']['ok'] else '#c94242',marker='o' if r['result']['ok'] else 'x')
    axes[1].set_yticks([0,1,2,3],['로그인 화면','홈 지도','여행 목록','여행 상세'])
    axes[1].set_ylim(-.6,3.6);axes[1].set_title('점 = 기대 화면 확인 · 실패는 빨간 ×',loc='left',fontsize=11)
    valid=[x for x in external if x.get('timings')]
    axes[2].plot([at(x['utc']) for x in valid],[x['timings']['total']*1000 for x in valid],color='#23896e',lw=.9,label='별도 새 연결 HTTP/2 전체 시간')
    axes[2].set_ylabel('응답 시간 (ms)');axes[2].legend(frameon=False,loc='upper left')
    labels={'http100':'HTTP 100','http450':'HTTP 450','hold192':'연결 유지','churn120':'연결 교체','mixed450':'혼합'}
    for ax in axes:
        for s in summary['stages']:
            ax.axvspan(at(s['startedAt']),at(s['endedAt']),color='#3663be',alpha=.06)
        ax.grid(axis='y',alpha=.18);ax.xaxis.set_major_formatter(dates.DateFormatter('%H:%M:%S',tz=kst))
    for i,s in enumerate(summary['stages']):
        axes[0].text(at(s['startedAt']),1.02+(i%2)*.1,labels[s['stage']],transform=axes[0].get_xaxis_transform(),fontsize=9)
    axes[-1].set_xlabel('2026-10-27 한국 시간 · 음영은 SSM 단계 제어 구간(실제 부하 구간은 JSON에 별도 기록)')
    fig.text(.11,.025,'기존 방어 유지 · 발생기 실패와 정상 접속 실패 구분 · LTE·OAuth·업로드 완료는 미검증',fontsize=10,color='#536176')
    fig.savefig(out/'01-connections-browser-external-ko.png',dpi=170);plt.close(fig)
    print(json.dumps({k:summary[k] for k in ['browserSamples','browserFailures','browserServerIds','sourceSeparation','appLoki','nftDelta']},ensure_ascii=False))


if __name__=='__main__':main()
