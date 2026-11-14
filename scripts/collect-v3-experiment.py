#!/usr/bin/env python3
"""Read-only bounded evidence. No traffic/faults or lifecycle changes; optional exact-ID AWS describe."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import time
import sys
import urllib.error
import urllib.parse
import urllib.request
sys.path.insert(0, str(Path(__file__).resolve().parent))
import v3_scaling_evidence as scaling
import v3_connect_recovery as recovery

APPS = {'frontend': 'frontend', 'auth-api': 'auth', 'trip-api': 'trip',
        'photo-api': 'photo', 'photo-converter': 'photo',
        'trip-result-worker': 'trip', 'ai-api': 'ai', 'ai-runner': 'ai', 'trip-websocket': 'trip'}
EVENTS = ['baseline_started', 'fault_injected', 'first_detected', 'fault_removed',
          'processing_resumed', 'backlog_cleared', 'observation_ended']
MAX_BYTES = 4 * 1024 * 1024
BE = 'job="backend",uri!~"/actuator.*"'
# Fixed queries only; no user expressions, CloudWatch scanning or arbitrary labels.
RANGE = {
    'scrape_up': 'min by (job) (up)',
    'scrape_targets': 'count by (job) (up)',
    'scrape_age_seconds': 'max by (job) (time() - timestamp(up))',
    'available_replicas': 'kube_deployment_status_replicas_available{namespace=~"frontend|auth|trip|photo|ai"}',
    'desired_replicas': 'kube_deployment_spec_replicas{namespace=~"frontend|auth|trip|photo|ai"}',
    'restarts_5m': 'sum by (namespace) (increase(kube_pod_container_status_restarts_total{namespace=~"frontend|auth|trip|photo|ai"}[5m]))',
    'cpu_cores_2m': 'sum by (namespace) (rate(container_cpu_usage_seconds_total{namespace=~"frontend|auth|trip|photo|ai",container!="",container!="POD"}[2m]))',
    'memory_bytes': 'sum by (namespace) (container_memory_working_set_bytes{namespace=~"frontend|auth|trip|photo|ai",container!="",container!="POD"})',
    'be_rps_2m': f'sum by (workload) (rate(http_server_requests_seconds_count{{{BE}}}[2m]))',
    'be_error_ratio_2m': f'(sum by (workload) (rate(http_server_requests_seconds_count{{{BE},status=~"5.."}}[2m])) or on(workload) (0 * sum by (workload) (rate(http_server_requests_seconds_count{{{BE}}}[2m])))) / sum by (workload) (rate(http_server_requests_seconds_count{{{BE}}}[2m]))',
    'be_p95_seconds_5m': f'histogram_quantile(0.95,sum by (le,workload) (rate(http_server_requests_seconds_bucket{{{BE}}}[5m])))',
    'ai_jobs': 'max by (state) (yeodam_ai_jobs{job="ai"})',
    'ai_p95_seconds_5m': 'histogram_quantile(0.95,sum by (le,operation,phase) (rate(yeodam_ai_operation_duration_seconds_bucket[5m])))',
    'cdc_probe': 'yeodam_cdc_probe_success',
    'cdc_observation_available': '(yeodam_cdc_probe_success == 1) and on(job, instance) (up{job="cdc-status"} == 1)',
    'cdc_connector': 'yeodam_cdc_connector_state and on(job, instance) (yeodam_cdc_probe_success == 1) and on(job, instance) (up{job="cdc-status"} == 1)',
    'cdc_task': 'yeodam_cdc_task_state and on(job, instance) (yeodam_cdc_probe_success == 1) and on(job, instance) (up{job="cdc-status"} == 1)',
    'cdc_task_present': 'yeodam_cdc_expected_task_present and on(job, instance) (yeodam_cdc_probe_success == 1) and on(job, instance) (up{job="cdc-status"} == 1)',
    'connect_restarts_total': 'kube_pod_container_status_restarts_total{namespace="kafka",pod="yeodam-v3-connect-connect-0",container="yeodam-v3-connect-connect"}',
    'connect_container_started': 'max(container_start_time_seconds{namespace="kafka",pod="yeodam-v3-connect-connect-0",container="yeodam-v3-connect-connect"})',
    'outbox_query_started': 'yeodam_outbox_query_started_timestamp_seconds',
    'outbox_query_completed': 'yeodam_outbox_query_completed_timestamp_seconds',
    'outbox_query_duration': 'yeodam_outbox_query_duration_seconds',
    'outbox_scrape_stamp': 'timestamp(yeodam_outbox_last_success_timestamp_seconds)',
    'outbox_alert_firing': 'ALERTS{alertname="V3ConversionDeliveryStalled",alertstate="firing"}',
    'outbox_probe': 'yeodam_outbox_probe_success',
    'outbox_sample_age_seconds': 'time() - yeodam_outbox_last_success_timestamp_seconds',
    'outbox_pending_raw': 'yeodam_outbox_pending',
    'outbox_pending_age_raw_seconds': 'yeodam_outbox_oldest_pending_age_seconds',
    'outbox_count_capped': 'yeodam_outbox_count_capped',
    'conversion_outbox_by_state': 'yeodam_conversion_outbox_pending',
    'conversion_outbox_age_by_state_seconds': 'yeodam_conversion_outbox_oldest_pending_age_seconds',
    'conversion_outbox_classification_capped': 'yeodam_conversion_outbox_classification_capped',
    'conversion_lag': 'keda_scaler_metrics_value{scaledObject="photo-converter"}',
}
LABELS = {'uid', 'pod', 'container', 'job', 'namespace', 'deployment', 'workload', 'operation', 'status', 'phase', 'state', 'task', 'connector',
          'outbox','category','alertname','alertstate','severity','scaledObject','metric','scaler','scalerIndex'}


def utc(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace('+00:00', 'Z')


def timestamp(value):
    d = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if d.tzinfo is None:
        raise ValueError('Use timestamps with explicit UTC offset')
    return int(d.timestamp())


def save(path, data):
    # Creation only; never overwrite prior evidence or follow an existing symlink.
    raw = json.dumps(data, indent=2, allow_nan=False) if not isinstance(data, str) else data
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as f:
        f.write(raw + '\n')


def kube_json(kubeconfig, *args):
    p = subprocess.run(['kubectl', '--kubeconfig', str(kubeconfig), '--request-timeout=20s',
                        *args, '-o', 'json'], capture_output=True, timeout=30)
    if p.returncode:
        raise ValueError('Kubernetes read failed; no command output exported')
    return json.loads(p.stdout)


def snapshot(kubeconfig, include_scaling=False):
    docs = kube_json(kubeconfig, 'get', 'deployments', '-A')['items']
    apps = []
    for d in docs:
        name = d['metadata']['name']; ns = d['metadata']['namespace']
        if APPS.get(name) != ns:
            continue
        apps.append({'namespace': ns, 'name': name, 'replicas': d['spec'].get('replicas', 1),
                     'podTemplateSha256': hashlib.sha256(json.dumps(d['spec']['template'], sort_keys=True).encode()).hexdigest(),
                     'containers': [{'name': c['name'], 'image': c['image'],
                                     'resources': c.get('resources', {}), 'kind': kind}
                                    for kind in ['containers', 'initContainers']
                                    for c in d['spec']['template']['spec'].get(kind, [])]})
    if {x['name'] for x in apps} != set(APPS):
        raise ValueError('Expected V3 app inventory missing; check kubeconfig')
    argo = kube_json(kubeconfig, '-n', 'argocd', 'get', 'application', 'yeodam-v3-apps')
    revision = argo.get('status', {}).get('sync', {}).get('revision')
    if not isinstance(revision, str) or not re.fullmatch(r'[0-9a-f]{40}', revision):
        raise ValueError('Approved Git revision unavailable')
    nodes = kube_json(kubeconfig, 'get', 'nodes')['items']
    capacity = {}
    for node in nodes:
        label = node['metadata'].get('labels', {}).get('node.kubernetes.io/instance-type', 'unknown')
        capacity[label] = capacity.get(label, 0) + 1
    result = {'nodeTypes': capacity, 'capturedAt': utc(datetime.now(timezone.utc).timestamp()),
            'apps': sorted(apps, key=lambda x: x['name']),
            'approvedRevision': revision,
            'source': 'current observation only; not historical configuration proof'}
    if include_scaling:
        result['scalingPolicies']=scaling.policies(lambda *args:kube_json(kubeconfig,*args))
        result['clusterUid']=scaling.CLUSTER_UID
    return result


def connect_runtime(kubeconfig):
    """Only exact Connect container metadata; never copy env, messages, logs or Secret fields."""
    pod = kube_json(kubeconfig, '-n', 'kafka', 'get', 'pod', 'yeodam-v3-connect-connect-0')
    meta = pod['metadata']; name = 'yeodam-v3-connect-connect'
    if meta.get('namespace') != 'kafka' or meta.get('name') != name+'-0':
        raise ValueError('Unexpected Connect identity')
    if not re.fullmatch(r'[a-f0-9-]{36}', meta.get('uid', '')):
        raise ValueError('Missing Connect Pod UID')
    containers = [c for c in pod['status'].get('containerStatuses', []) if c['name'] == name]
    if len(containers) != 1:
        raise ValueError('Connect container state unavailable')
    c = containers[0]; count = c.get('restartCount')
    if type(count) is not int or count < 0:
        raise ValueError('Invalid restart count')
    container_id = c.get('containerID') or None
    if container_id and not re.fullmatch(r'containerd://[a-f0-9]{64}', container_id):
        raise ValueError('Unexpected runtime container identity')
    started = c.get('state', {}).get('running', {}).get('startedAt')
    previous = c.get('lastState', {}).get('terminated', {})
    for value in [started, previous.get('startedAt'), previous.get('finishedAt')]:
        if value is not None:timestamp(value)
    reason = previous.get('reason')
    if reason is not None and reason not in ('Completed', 'Error', 'OOMKilled', 'ContainerCannotRun'):
        reason = 'other'
    return {'podUid': meta['uid'], 'containerId': container_id, 'restartCount': count,
            'ready': c.get('ready') is True, 'startedAt': started,
            'lastTermination': {'reason': reason, 'exitCode': previous.get('exitCode'),
                                'startedAt': previous.get('startedAt'), 'finishedAt': previous.get('finishedAt')}}


def observe_connect(kubeconfig, output, duration, interval):
    if not 120 <= duration <= 1800 or not 5 <= interval <= 30:
        raise ValueError('Use a bounded 2-30 minute observation at 5-30 second intervals')
    identity = kube_json(kubeconfig, 'get', 'namespace', 'kube-system')
    if identity.get('metadata', {}).get('uid') != scaling.CLUSTER_UID:
        raise ValueError('Wrong V3 cluster identity')
    out = Path(output); out.mkdir(mode=0o700, parents=True, exist_ok=False)
    started = time.monotonic(); samples = []; first = last = None; changed = False
    while True:
        try:
            state = connect_runtime(kubeconfig)
            if first is None:first = state
            if last is not None:
                changed |= any(state[k] != last[k] for k in ('podUid','containerId','restartCount','startedAt'))
            last = state
            row = {'observedAt': utc(datetime.now(timezone.utc).timestamp()), 'status': 'ok', **state}
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
            row = {'observedAt': utc(datetime.now(timezone.utc).timestamp()), 'status': 'unknown'}
        save(out/f'sample-{len(samples):05d}.json', row); samples.append(row)
        remaining = duration-(time.monotonic()-started)
        if remaining <= 0:break
        time.sleep(min(interval,remaining))
    gaps = sum(x['status'] != 'ok' for x in samples)
    result = {'clusterUid': scaling.CLUSTER_UID, 'collectionStatus': 'partial' if gaps else 'complete', 'samples': len(samples),
              'observationGaps': gaps, 'containerLifecycleChanged': changed,
              'restartDelta': None if first is None or last is None or first['podUid'] != last['podUid'] else last['restartCount']-first['restartCount'],
              'podReplaced': None if first is None or last is None else first['podUid'] != last['podUid'],
              'restartCause': 'not_assessed; correlate exact kubelet and previous-container logs',
              'noRestartProven': False, 'verdict': 'business_recovery_not_assessed'}
    # Polling can miss a deleted/replaced intermediate Pod. Never claim continuous no-restart proof.
    save(out/'result.json', result)
    return result


def connect_events(kubeconfig, output, start, end):
    """Read current container logs in memory, return only reviewed assignment fields."""
    # Validate before any cluster call. The parser also enforces the response size.
    recovery.parse_events('',start,end)
    if kube_json(kubeconfig,'get','namespace','kube-system').get('metadata',{}).get('uid')!=scaling.CLUSTER_UID:
        raise ValueError('Wrong V3 cluster identity')
    before=connect_runtime(kubeconfig)
    from v3_cdc_fault import Kube
    raw=Kube(kubeconfig).call(['-n','kafka','logs','yeodam-v3-connect-connect-0',
                             '-c','yeodam-v3-connect-connect','--since-time='+start,
                             '--timestamps=true','--tail=5000','--limit-bytes='+str(recovery.MAX_BYTES)])
    result=recovery.parse_events(raw,start,end)
    after=connect_runtime(kubeconfig)
    unchanged=all(before[k]==after[k]for k in ('podUid','containerId','restartCount','startedAt'))
    result.update(capturedAt=utc(datetime.now(timezone.utc).timestamp()),clusterUid=scaling.CLUSTER_UID,
                  containerBefore=before,containerAfter=after,containerUnchangedDuringRead=unchanged,
                  collectionStatus='bounded' if unchanged else 'partial',
                  logByteLimitReached=len(raw.encode())>=recovery.MAX_BYTES,
                  start=start,end=end,firstDeliveryAssessed=False)
    # Raw messages and literal client IDs/URLs are intentionally not exported.
    save(Path(output),result)
    return result


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('Prometheus redirect refused')


class Prometheus:
    def __init__(self, endpoint):
        p = urllib.parse.urlsplit(endpoint)
        if (p.scheme != 'http' or p.hostname not in {'127.0.0.1', '[::1]', '::1'}
                or p.username or p.password or p.path not in {'', '/'} or p.query or p.fragment):
            raise ValueError('Use an explicit localhost HTTP port-forward, without credentials/path')
        if not p.port:
            raise ValueError('Explicit local port required')
        self.endpoint = endpoint.rstrip('/')
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def query(self, expression, start, end, step=None):
        params = {'query': expression, 'timeout': '8s'}
        if step:
            path = 'query_range'; params.update(start=start, end=end, step=step)
        else:
            path = 'query'; params['time'] = end
        url = self.endpoint + '/api/v1/' + path + '?' + urllib.parse.urlencode(params)
        try:
            with self.opener.open(url, timeout=12) as r:
                raw = r.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise ValueError('Response size bound exceeded')
            j = json.loads(raw)
            if j.get('status') != 'success':
                raise ValueError('Prometheus query rejected')
            data = j['data']
            expected = 'matrix' if step else 'vector'
            if data.get('resultType') != expected or len(data['result']) > 100:
                raise ValueError('Unexpected or oversized result')
            series = []; nonfinite = 0
            for s in data['result']:
                points = s['values'] if step else [s['value']]
                if len(points) > 721:
                    raise ValueError('Point bound exceeded')
                labels = {k: v for k, v in s['metric'].items() if k in LABELS}
                values = []
                for t, v in points:
                    n = float(v)
                    if not math.isfinite(n): nonfinite += 1
                    values.append([t, n if math.isfinite(n) else None])
                series.append({'labels': labels, 'values': values})
            return {'status': 'partial' if j.get('warnings') or j.get('infos') else 'ok' if series else 'missing',
                    'nonFiniteSamples': nonfinite,
                    'annotationCount': len(j.get('warnings', [])) + len(j.get('infos', [])),
                    'series': series}
        except (OSError, ValueError, KeyError, TypeError) as error:
            if isinstance(error, urllib.error.HTTPError): error.close()
            # Do not copy server error bodies/URLs or credentials into evidence.
            return {'status': 'query_failed', 'errorType': type(error).__name__, 'series': []}


def counts_and_percentiles(client, start, end):
    window = str(end-start) + 's'
    be_count = f'sum by (workload) (increase(http_server_requests_seconds_count{{{BE}}}[{window}]))'
    ai_count = f'sum by (operation,phase) (increase(yeodam_ai_operation_duration_seconds_count[{window}]))'
    expressions = {
        'be_count_estimate': be_count,
        'be_error_ratio': f'(sum by (workload) (increase(http_server_requests_seconds_count{{{BE},status=~"5.."}}[{window}])) or on(workload) (0 * ({be_count}))) / ({be_count})',
        'be_p95_seconds': f'histogram_quantile(0.95,sum by (le,workload) (increase(http_server_requests_seconds_bucket{{{BE}}}[{window}])))',
        'ai_count_estimate': ai_count,
        'ai_p95_seconds': f'histogram_quantile(0.95,sum by (le,operation,phase) (increase(yeodam_ai_operation_duration_seconds_bucket[{window}])))',
    }
    return {name: dict(client.query(expr, start, end), expression=expr)
            for name, expr in expressions.items()}


def annotate_samples(summary, min_samples=None):
    for kind, keys in [('be', {'workload'}), ('ai', {'operation', 'phase'})]:
        counts = summary[kind+'_count_estimate']; p95 = summary[kind+'_p95_seconds']
        index = {tuple(sorted(x['labels'].items())): x['values'][0][1] for x in counts['series']}
        for s in p95['series']:
            n = index.get(tuple(sorted((k, v) for k, v in s['labels'].items() if k in keys)))
            value = s['values'][0][1]
            s['countEstimate'] = n
            s['sampleStatus'] = ('unknown' if n is None else 'no_traffic' if n == 0
                                 else 'adequacy_unassessed' if min_samples is None
                                 else 'low_sample' if n < min_samples else 'threshold_met')
            if n is None or n <= 0:
                s['values'][0][1] = None
            elif value is None:
                s['sampleStatus'] = 'histogram_unavailable'
    return summary


def validate_events(path, start, end):
    if not path:
        return []
    events = json.loads(Path(path).read_text())
    if not isinstance(events, list) or len(events) > len(EVENTS):
        raise ValueError('Expected bounded event list')
    seen = set(); previous = start
    for e in events:
        if set(e) != {'event', 'at'} or e['event'] not in EVENTS or e['event'] in seen:
            raise ValueError('Unknown/duplicate event or unexpected field')
        t = timestamp(e['at'])
        if not previous <= t <= end:
            raise ValueError('Events must be ordered and inside collection window')
        e['at'] = utc(t); previous = t; seen.add(e['event'])
    return events


def markdown(result):
    lines = ['# V3 실험 관측 결과', '',
             f"- 구간: {result['start']} ~ {result['end']} (UTC)",
             f"- 수집 상태: {result['collectionStatus']}",
             f"- 실험 전후 설정 일치: {result['conditionsMatch']}",
             f"- 이미지·앱 설정 일치: {result['conditionsComparison']['appConfigurationMatch']}",
             f"- 자동 확장 정책 일치: {result['conditionsComparison']['scalingPolicyMatch']} (미수집 시 null)",
             f"- Pod 수 변화: {result['conditionsComparison']['replicaChanges']}",
             f"- Node 종류별 수 변화: {result['conditionsComparison']['nodeTypesChanged']}",
             f"- CDC 상태 관측: {result.get('cdcObservation', 'not_collected')}",
             '- 업무 성공/누락/중복/복구 목표 판정: 미수행. 별도 업무 결과 대조 및 합의 필요.',
             '', '| 전체 구간 지표 | 상태 | 시계열 수 |', '| --- | --- | --- |']
    for name, data in result['wholeWindow'].items():
        lines.append(f"| {name} | {data['status']} | {len(data['series'])} |")
    lines += ['', '| 지표 | 구분 | 값 | 건수 추정 / 표본 상태 |', '| --- | --- | ---: | --- |']
    for name, data in result['wholeWindow'].items():
        for row in data['series']:
            labels = ', '.join(k+'='+str(v) for k,v in sorted(row['labels'].items()))
            value = row['values'][0][1]
            display = '관측 불가' if value is None else f'{value:.6g}'
            samples = str(row.get('countEstimate', '-'))+' / '+row.get('sampleStatus', '-')
            lines.append(f'| {name} | {labels} | {display} | {samples} |')
    lines += ['', '## 수동 기록 시각', '']
    lines += [f"- {e['event']}: {e['at']}" for e in result['events']] or ['- 기록 없음. 장애/탐지/복구 시각을 추정하지 않음.']
    lines += ['', '## 해석 경계', '',
              '- increase 기반 건수는 스크레이프 보간 추정치이며 DB의 정확한 업무 건수가 아님.',
              '- 전체 구간 p95는 구간 Histogram으로 계산. 시간별 p95 평균이나 단계 p95 합산 미사용.',
              '- 시간별 지표는 trailing 2m/5m로 구간 시작 전 자료가 포함될 수 있음.',
              '- NaN/Inf는 null, 빈 결과는 missing, 조회 오류는 query_failed. 정상 0으로 대체하지 않음.',
              '- 표본 기준은 기본 미정. minSamples가 명시된 경우에만 해당 기준 미달을 표시하며 SLO 통과와 구분.',
              '- 설정은 시작 전과 수집 시점 관찰값. 중간 변경/과거 상태 일치를 보장하지 않음.',
              '- 엄격한 시작/끝 일치와 정적 설정 일치를 구분. Pod/Node 변화 자체를 승인된 확장 성공으로 판정하지 않음.',
              '- Outbox 원시 PENDING 집계는 시험 작업 전용/활성 작업 수가 아님. 성공 작업의 발행 확인 잔여가 포함될 수 있음.',
              '- Outbox probe·표본 나이·count_capped를 함께 확인. Kafka lag=0만으로 상류 작업 없음 판정 금지.',
              '- CDC REST 상태는 마지막 보고값일 수 있음. probe와 수집 성공 조건을 적용하고 공백은 UNKNOWN으로 해석.',
              '- Pod UID 유지가 무재시작 증거는 아님. Connect 재시작 수·컨테이너 시작 시각과 observe-connect 기록을 대조.',
              '- 샘플 간 탐지/복구 시각과 업무 손실/중복을 자동 추정하지 않음.',
              '- AI total은 AI 측 구간이며 BE 요청부터 최종 DB 반영까지의 시간이 아님.']
    return '\n'.join(lines)


def cdc_observation_summary(data, start, end, step=30):
    expected = set(range(start, end+1, step))
    # More than one exporter series is ambiguous; do not combine independent observers.
    readable = set()
    if data['status'] == 'ok' and len(data['series']) == 1:
        readable = {t for t, value in data['series'][0]['values'] if t in expected and value == 1}
    return {'expectedGridPoints':len(expected),'readableGridPoints':len(readable),
            'unknownGridPoints':len(expected-readable),
            'status':'observed' if readable == expected else 'unknown_intervals',
            'meaning':'REST readability at the query grid only; neither current Task liveness nor delivery proof'}


def collect(args):
    start, end = timestamp(args.start), timestamp(args.end)
    if not 120 <= end-start <= 21600 or end > int(datetime.now(timezone.utc).timestamp()):
        raise ValueError('Use a completed window between 2 minutes and 6 hours')
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', args.experiment_id):
        raise ValueError('Use an opaque experiment ID, not user data')
    events = validate_events(args.events, start, end)
    before_raw = Path(args.conditions).read_bytes(); before = json.loads(before_raw)
    if timestamp(before['capturedAt']) > start:
        raise ValueError('Conditions snapshot must precede collection window')
    after = snapshot(args.kubeconfig, include_scaling=True) if 'scalingPolicies'in before else snapshot(args.kubeconfig)
    input_identity=scaling.input_summary(getattr(args,'input_manifest',None))
    client = Prometheus(args.prometheus)
    out = Path(args.output)
    out.mkdir(mode=0o700, parents=True, exist_ok=False)
    ranges = {k: dict(client.query(v, start, end, 30), expression=v) for k, v in RANGE.items()}
    whole = annotate_samples(counts_and_percentiles(client, start, end), args.min_samples)
    status = 'complete' if all(x['status']=='ok' for x in [*ranges.values(), *whole.values()]) else 'partial'
    result = {'schemaVersion': 2, 'experimentId': args.experiment_id, 'start': utc(start), 'end': utc(end),
              'capturedAt': utc(datetime.now(timezone.utc).timestamp()), 'stepSeconds': 30,
              'collectionStatus': status, 'minSamples': args.min_samples, 'events': events, 'conditionsBefore': before,
              'conditionsAfter': after, 'conditionsSha256': hashlib.sha256(before_raw).hexdigest(),
              'conditionsMatch': all(before[k]==after[k] for k in ['apps', 'nodeTypes', 'approvedRevision']),
              'conditionsComparison':scaling.compare_conditions(before,after),'input':input_identity,
              'wholeWindow': whole, 'ranges': ranges, 'verdict': 'not_assessed'}
    result['cdcObservation'] = cdc_observation_summary(ranges['cdc_observation_available'],start,end)
    save(out/'result.json', result); save(out/'summary.md', markdown(result))
    print(json.dumps({'collectionStatus': status, 'conditionsMatch': result['conditionsMatch'],
                      'verdict': 'not_assessed'}))
    return 0 if status=='complete' else 2


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    snap = commands.add_parser('snapshot'); snap.add_argument('--kubeconfig', required=True); snap.add_argument('--output', required=True)
    snap.add_argument('--include-scaling',action='store_true',help='Read fixed HPA/KEDA and api/worker pool policy hashes')
    c = commands.add_parser('collect')
    for name in ['kubeconfig', 'conditions', 'start', 'end', 'output', 'experiment-id', 'prometheus']:
        c.add_argument('--'+name, required=True)
    c.add_argument('--events')
    c.add_argument('--min-samples', type=int, help='Optional agreed sample threshold; no default adequacy claim')
    c.add_argument('--input-manifest',help='Private JPEG manifest; export only aggregate identity')
    inp=commands.add_parser('input-manifest',help='Hash an explicit JPEG selection locally; no upload or EXIF export')
    inp.add_argument('--root',required=True);inp.add_argument('--selection',required=True);inp.add_argument('--inspection');inp.add_argument('--expected-count',type=int,default=20);inp.add_argument('--output',required=True)
    watch=commands.add_parser('observe-scaling',help='Read converter/HPA/worker lifecycle only; never generate load or mutate resources')
    watch.add_argument('--kubeconfig',required=True);watch.add_argument('--experiment-id',required=True);watch.add_argument('--duration-seconds',type=int,required=True);watch.add_argument('--interval-seconds',type=int,default=15);watch.add_argument('--input-manifest');watch.add_argument('--output',required=True)
    conn=commands.add_parser('observe-connect',help='Read exact Connect container lifecycle; no logs, exec, Secrets or restarts')
    conn.add_argument('--kubeconfig',required=True);conn.add_argument('--output',required=True);conn.add_argument('--duration-seconds',type=int,required=True);conn.add_argument('--interval-seconds',type=int,default=10)
    ce=commands.add_parser('connect-events',help='Read bounded exact-Connect logs; export only group/assignment timestamps, never raw logs or Secrets')
    for name in ['kubeconfig','output','start','end']:ce.add_argument('--'+name,required=True)
    verify=commands.add_parser('verify-node-release',help='Optional AWS describe of removed new worker IDs only; no lifecycle commands')
    verify.add_argument('--observation',required=True);verify.add_argument('--output',required=True)
    args = parser.parse_args()
    try:
        if args.command=='snapshot':
            save(Path(args.output), snapshot(args.kubeconfig,args.include_scaling)); return 0
        if args.command=='input-manifest':
            data=scaling.input_manifest(args.root,args.selection,args.inspection,args.expected_count);scaling.save(args.output,data)
            print(json.dumps(scaling.input_summary(args.output)));return 0
        if args.command=='observe-scaling':
            result=scaling.observe(lambda *items:kube_json(args.kubeconfig,*items),args.output,args.experiment_id,args.duration_seconds,args.interval_seconds,scaling.input_summary(args.input_manifest))
            print(json.dumps({k:result[k]for k in ['collectionStatus','sampleCount','oneTwoOneObserved','twoReadyOnDistinctNodesObserved','observationGaps','policyOrRevisionChanged','ec2TerminationVerified','verdict']}));return 0 if result['collectionStatus']=='complete'else 2
        if args.command=='observe-connect':
            result=observe_connect(args.kubeconfig,args.output,args.duration_seconds,args.interval_seconds);print(json.dumps(result));return 0 if result['collectionStatus']=='complete'else 2
        if args.command=='connect-events':
            result=connect_events(args.kubeconfig,args.output,args.start,args.end);print(json.dumps({'collectionStatus':result['collectionStatus'],'events':len(result['events']),'rawLogsPersisted':False}));return 0 if result['containerUnchangedDuringRead']else 2
        if args.command=='verify-node-release':
            result=scaling.verify_node_release(args.observation);scaling.save(args.output,result);print(json.dumps(result));return 0 if result.get('allNewWorkersTerminated')is True else 2
        if args.min_samples is not None and args.min_samples < 1:
            raise ValueError('Sample threshold must be positive')
        return collect(args)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        print('Evidence collection rejected; check inputs/access. No raw diagnostic exported.', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
