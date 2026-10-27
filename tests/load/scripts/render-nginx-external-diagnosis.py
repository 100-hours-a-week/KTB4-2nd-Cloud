#!/usr/bin/env python3
"""Offline #438 comparison, request-ID reconciliation and Korean plots from archived sources."""
import argparse
from collections import Counter
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import statistics
import tempfile
from zoneinfo import ZoneInfo


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--archive',type=Path,required=True)
    p.add_argument('--font',type=Path,required=True)
    a=p.parse_args();out=a.archive/'plots';out.mkdir(exist_ok=False)
    def load(name):return json.loads((a.archive/name).read_text())
    def rows(name):return [json.loads(x) for x in (a.archive/name).read_text().splitlines()]
    external=rows('external/samples.jsonl');ena=rows('app/ena-samples.jsonl');host=rows('app/samples.jsonl')
    app=rows('app/access.jsonl');loki=rows('plg/loki-access.jsonl')
    appids=Counter(x['request_id'] for x in app if x.get('request_id'))
    lokiids=Counter(x['data']['request_id'] for x in loki if x.get('data',{}).get('request_id'))
    stages=load('trials-validated/stages.json')+load('trials-comparison/stages.json')
    normal=load(str(next((a.archive/'app').glob('normal-*-summary.json')).relative_to(a.archive)))
    summary={'external':load('external/summary.json'),'ena':load('app/ena-summary.json'),
       'host':load('app/result.json'),'app':load('app/index.json'),'normal':normal,
       'requestIds':{'app':sum(appids.values()),'loki':sum(lokiids.values()),
          'missing':sum((appids-lokiids).values()),'extra':sum((lokiids-appids).values())},
       'stages':stages,'tcp':{},'http':{},
       'externalLatencyMs':{'median':statistics.median(x['timings']['total']*1000 for x in external if 'timings' in x),
                           'max':max(x['timings']['total']*1000 for x in external if 'timings' in x)},
       'limitations':['No reproduction of the reported LTE failure; Mac is a different network.',
         'HTTP450 uses 5s preparation + 5s ramp + 20s target, not 30s constant 450RPS.',
         'Mixed HTTP aborted during 20RPS preparation on generator-source timeout; never reached 450RPS.',
         'Original instructor lifetime ENA counters cannot be attributed to the original load.',
         'No raw SYN-only traffic or precise original generator pattern was replayed.',
         'External probe uses HTTP/1.1; browser HTTP/2 behavior is not covered.']}
    for f in sorted((a.archive/'generator').glob('*-tcp.json')):
        v=json.loads(f.read_text());summary['tcp'][f.name]={k:v[k] for k in ['attempted','opened','httpStatuses','errors','peakGeneratorSockets','recoveryProbes']}
    for f in sorted((a.archive/'generator').glob('*-http-summary.json')):
        v=json.loads(f.read_text());m=v['metrics'];summary['http'][f.name]={k:m[k] for k in ['http_reqs','http_req_failed','dropped_iterations'] if k in m}
    (out/'analysis.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
    os.environ.setdefault('MPLCONFIGDIR',tempfile.mkdtemp(prefix='nginx438-plot-'))
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib import font_manager,dates
    font_manager.fontManager.addfont(str(a.font))
    plt.rcParams.update({'font.family':font_manager.FontProperties(fname=a.font).get_name(),
       'axes.unicode_minus':False,'font.size':12,'axes.spines.top':False,'axes.spines.right':False})
    kst=ZoneInfo('Asia/Seoul')
    def at(s):return datetime.fromisoformat(s).astimezone(kst)
    def new(title):
        fig,axes=plt.subplots(2,1,figsize=(12.8,7.2),sharex=True)
        fig.subplots_adjust(left=.085,right=.97,bottom=.16,top=.85,hspace=.38)
        fig.suptitle(title,x=.085,y=.97,ha='left',fontsize=20,weight='bold')
        return fig,axes
    def save(fig,axes,name,note):
        for ax in axes:
            ax.grid(axis='y',alpha=.2);ax.set_ylim(bottom=0)
            ax.xaxis.set_major_formatter(dates.DateFormatter('%H:%M',tz=kst))
            for i,stage in enumerate(stages):
                ax.axvspan(at(stage['startedAt']),at(stage['endedAt']),color='#2256c5',alpha=.04 if i%2 else .1)
        for stage in stages:
            labels={'http100':'HTTP100','http450':'HTTP450','hold192':'연결 유지','churn120':'연결 교체','mixed450':'혼합'}
            axes[0].text(at(stage['startedAt']),1.02,labels[stage['stage']],
                transform=axes[0].get_xaxis_transform(),fontsize=9,color='#526078')
        axes[-1].set_xlabel('2026-10-27 한국 시간 · 음영은 단계 실행 제어 창')
        fig.text(.085,.025,note,fontsize=10,color='#526078');fig.savefig(out/name,dpi=180);plt.close(fig)
    fig,axes=new('독립 외부 접속의 실제 응답과 연결 단계')
    for path,label,color in [('/login','로그인 화면','#2256c5'),('/api/actuator/health','BE 헬스','#249487')]:
        r=[x for x in external if x.get('path')==path and 'timings' in x]
        axes[0].plot([at(x['utc']) for x in r],[x['timings']['total']*1000 for x in r],'.-',ms=3,lw=.8,label=label,color=color)
    axes[0].set_ylabel('전체 시간 (ms)');axes[0].legend(frameon=False,ncol=2)
    r=[x for x in external if 'timings' in x]
    for key,label,color in [('tcp','TCP 완료','#2256c5'),('tls','TLS 완료','#d98223')]:
        axes[1].plot([at(x['utc']) for x in r],[x['timings'][key]*1000 for x in r],lw=.8,label=label,color=color)
    axes[1].set_ylabel('요청 시작 이후 (ms)');axes[1].legend(frameon=False,ncol=2)
    save(fig,axes,'01-external-stages-ko.png','Mac 외부 출처 · 새 TCP/TLS 연결 · 화면 렌더링·LTE 경로·로그인 완료의 증거와 구분')
    fig,axes=new('ENA 제한 증분과 Nginx 연결을 대조했다')
    keys=[('bw_in_allowance_exceeded','수신 대역폭'),('bw_out_allowance_exceeded','송신 대역폭'),('pps_allowance_exceeded','PPS')]
    for key,label in keys:
        axes[0].plot([at(x['utc']) for x in ena],[x.get('counters',{}).get(key,math.nan)-summary['ena']['firstCounters'][key] for x in ena],label=label,lw=1.5)
    axes[0].set_ylabel('시작 대비 누적 증분');axes[0].legend(frameon=False,ncol=3)
    if not any(summary['ena']['counterDelta'].values()):
        axes[0].set_ylim(0,1);axes[0].set_yticks([0,1])
        axes[0].text(.98,.7,'ENA '+str(summary['ena']['sampleCount'])+'개 표본 · 수집 오류 '+str(summary['ena']['errors'])+'\n수신·송신·PPS 제한 증분 각각 0',ha='right',transform=axes[0].transAxes)
    axes[1].plot([at(x['utc']) for x in host],[x.get('active') for x in host],color='#2256c5',label='Nginx Active')
    axes[1].set_ylabel('활성 연결');axes[1].legend(frameon=False)
    save(fig,axes,'02-ena-connections-ko.png','시작 전 누적값을 빼고 표시 · 관측 공백을 0으로 바꾸지 않음 · 오전 공격의 ENA 증분은 여전히 미확인')
    (out/'source-hashes.json').write_text(json.dumps({str(f.relative_to(a.archive)):hashlib.sha256(f.read_bytes()).hexdigest()
      for f in [a.archive/'external/samples.jsonl',a.archive/'app/ena-samples.jsonl',a.archive/'app/samples.jsonl',a.archive/'app/access.jsonl']},indent=2))
    print(json.dumps({'requestIds':summary['requestIds'],'external':summary['external'],
                     'normal':normal,'enaDelta':summary['ena']['counterDelta']},ensure_ascii=False))


if __name__=='__main__':main()
