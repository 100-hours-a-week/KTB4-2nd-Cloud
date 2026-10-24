# V1 최종 재부하의 수집·캡처·사후 분석 — #426

현재 V1 스테이징의 조회5r/s·burst30·nodelay/429와 client Keepalive15초를 관측한다.
이 도구는 부하를 발생시키거나 Nginx·FE·BE·DB 설정을 바꾸지 않는다.
강사 본시험은 준비 검증 이후 별도로 진행한다. 수집 성공과 방어 성공을 구분한다.

## 자료와 보존 위치

| 자료 | 수집 경로 | 사후 확인 범위 |
|---|---|---|
|App 연결·CPU·메모리·FD·디스크|App의1초 수집기|미리 켜지 않았다면 소급 복원 불가|
|Nginx 접근·오류 원본|App root 전용 실행 폴더|기존 파일을 수정하지 않고 inode를 따라 복사|
|대시보드 지표·Nginx 로그|기존 Prometheus·Loki|7일 보존 설정 내 절대시간 조회|
|V1 BE·AI 로그|CloudWatch v1-source/backend·ai|실제14일 보존, 이벤트/수집 시각 모두 기록|
|Grafana 화면|로그인된 브라우저 PNG|나중에도 같은 시간대로 재캡처 가능|
|별도 아카이브|로컬 JSON/JSONL·CSV·PNG·SHA256|PLG 보존 기간 이후에도 오프라인 분석|

서버/볼륨 장애까지 App 로컬 보관만으로 보호하지는 않는다. 시험 후 아카이브를 내려받는다.
원본 로그와 HMAC키는 App root 전용으로 유지한다. 외부 반출본은 IP를 HMAC으로 바꾸고 인증 정보를 제외한다.
화면 캡처에서는 원본 접근 로그 패널을 가리고13개 그래프를 촬영한다.
Mac이 꺼지면 브라우저 캡처는 실행되지 않지만, 이미 시작한 App 수집기는 계속 기록한다.
후일 재구성한 PNG와 당시 실시간 스크린샷은 별도 표기한다.

## 실행 전 확인

Account483175530259 / ap-northeast-2. App `i-05d320e3a87200d5a`,
PLG `i-05c76ccb2b53f9279`, 기존 발생기 `i-0c53b65eb35dd2b42`를 사용한다.
대상은 `https://v1-staging.yeodam-2gether.com`이다.

1. App·PLG 헬스, 수집 최신성, 시각, 디스크, 현재 Nginx SHA, 이미지와 동시 작업을 확인한다.
2. 강사에게 시작/종료를 알려달라고 요청한다. 가능하면 발생기의 총 요청·실패·시간·RPS/연결 수도 받는다.
   서버 로그만으로 서버에 도달하지 않은 요청 수를 계산하지 않는다.
3. 정상 조회와 업로드의 합성 세션을 구분해 시험 직전에 발급한다. 기존30분 TTL은 유지한다.
   업로드는 잔여15분 이상을 확인한다. 토큰 만료와 방어 실패를 구분한다.
4. 발생기의 기존 `run-k6.sh`, `view-baseline.js`, `upload-baseline.js`와 고정150장 Manifest를 쓴다.
   결과 폴더에 이번 실행UUID를 포함한다. 사진150장/383,664,318B Manifest SHA는
   `7de807ea1a3a79f4bb01d010371054218945817686f5f3d5aa3f4e31cbe745e3`다.

도구는 두 서버의 `/opt/yeodam/ops/nginx-rehearsal-426/`에 설치했다.
App에는 capture/observer/archive/audit Python이 있고 PLG에는 archive Python이 있다.
수정 시 검토한 동일 소스를 SSM으로 전용 폴더에 전달한다. Token/.env는 배포하지 않는다.

## 1. 서버 수집 시작

다음은 **App SSM/root**에서 실행한다. UUID는 이번 시험 전체에서 한 번만 만든다.

```bash
sudo -i
RUN_ID="$(python3 -c 'import uuid; print(uuid.uuid4())')"
SITE_SHA="$(sha256sum /etc/nginx/sites-enabled/yeodam-v1-staging | cut -d' ' -f1)"
python3 /opt/yeodam/ops/nginx-rehearsal-426/nginx-rehearsal.py start \
  --run-id "$RUN_ID" --seconds 1800 --expected-site-sha256 "$SITE_SHA"
```

SHA를 비교 완료된15초 설정의 `070fab168a37a232747a33ba46a3b6282fb998235ecc70adcff4e6ffc8e05a86`과
대조한다. 다르면 차이를 읽고 진행한다. 새로운 Hash를 무조건 신뢰하는 절차가 아니다.
출력UUID를 따로 저장하면 SSM 연결은 종료해도 된다.

systemd 임시Unit `yeodam-nginx426-<UUID>`로 동작한다. 기본30분/최대60분,
Nice10·MemoryMax128MiB·CPUQuota20%를 설정했다. 원본 로그 상한512MiB, root80%에서 중단한다.
한도 중단·미복사 bytes·표본 오류는 불완전으로 남기고 기존 증거를 자동 삭제하지 않는다.
이 한도는 강사의 부하를 중지하지 않는다. 필요하면 강도 조정/중단을 별도 요청한다.

```bash
python3 /opt/yeodam/ops/nginx-rehearsal-426/nginx-rehearsal.py status --run-id "$RUN_ID"
python3 /opt/yeodam/ops/nginx-rehearsal-426/nginx-rehearsal.py mark --run-id "$RUN_ID" --phase baseline
```

checkpoint의 updatedAt/sampleCount가 진행하는지 본다. 실패 시 해당 Unit의 journal을 확인한다.
자료는 `/var/lib/yeodam/nginx-rehearsal/<UUID>/`에 남는다. `.private.raw` 원본은 외부로 보내지 않는다.

## 2. 정상 경로와 시험 구간 연결

신선한 read fixture 세션UUID를 사용해 **App SSM/root**에서 실행한다.

```bash
SESSION_RUN_ID='<이번 read fixture 세션UUID>'
python3 /opt/yeodam/ops/nginx-rehearsal-426/nginx-rehearsal.py normal-start \
  --run-id "$RUN_ID" --session-run-id "$SESSION_RUN_ID" --seconds 1200
```

별도 Unit에서2초에1회 공개HTTPS 경유 지도·목록·상세를 순환한다. 같은 합성 계정의 API 확인이며,
실제 브라우저/여러 사용자 시험은 아니다. 비200 또는 초기1초 지연 가드 초과 시 probe만 중단한다.
수동 중단과 실행 시간 종료는 정상 종료 사유로 구분한다. 증거 수집기는 계속 기록한다.
최장25분이고 시작 시 실행 시간+60초의 Token 잔여시간을 검사한다. status의 normalResult로 확인한다.
로그인·실제 화면 확인은 별도 사용자로 수행한다.

150장 실업로드는 기존 k6 시나리오와 스테이징 전용 세션파일로 실행한다. 토큰을 인자/채팅에 노출하지 않는다.
실제 부하와 겹친 시간, 업로드·AI·조회 완료 시각을 기록한다. 짧은 중첩을 전체 여정의 부하로 표현하지 않는다.

```bash
# App SSM/root. 실제 강사 부하 시작/종료에 맞춰 기록한다.
python3 /opt/yeodam/ops/nginx-rehearsal-426/nginx-rehearsal.py mark --run-id "$RUN_ID" --phase load-start
python3 /opt/yeodam/ops/nginx-rehearsal-426/nginx-rehearsal.py mark --run-id "$RUN_ID" --phase load-end
# 정상 작업 완료와 회복 확인 후
python3 /opt/yeodam/ops/nginx-rehearsal-426/nginx-rehearsal.py mark --run-id "$RUN_ID" --phase recovery-end
python3 /opt/yeodam/ops/nginx-rehearsal-426/nginx-rehearsal.py stop --run-id "$RUN_ID"
```

stop은 자신이 시작한 수집과 정상probe만 중단한다. result.json의 collectionComplete/siteUnchanged,
종료 사유·미복사 bytes·precisionWarning을 확인한다.1초 초과 스케줄 지연도 원본에 남긴다.
FD 수와 worker_connections 점유율은 다르다. 수집기 자신의 stub 요청·exporter·정상probe도
전체 연결/요청 카운터에 포함되므로 전체 증가를 모두 공격 요청으로 계산하지 않는다.

## 3. DB/S3 원본 대조

**App SSM/root**에서 실제 업로드를 만든 합성 소유자와 동일 Manifest를 지정한다.
Manifest JSON만 전용 폴더에 전달하면 되고, App에 사진 본체를 옮길 필요는 없다.

```bash
python3 /opt/yeodam/ops/nginx-rehearsal-426/audit-nginx-rehearsal-upload.py \
  --run-id "$RUN_ID" --session-run-id '<업로드용 세션UUID>' \
  --trip-id '<실제 여행ID>' --manifest '<동일150장 Manifest 절대경로>'
```

정확한 합성 이메일/소유자를 검사하고 DB 읽기와 S3 HEAD만 수행한다. COMPLETED·DB150·S3원본150,
총크기·크기 다중집합을 확인한다. 본문checksum 비교는 아니다. 다른 여행을 증거로 대체하지 않는다.
작업 후 자신이 발급한 read/upload SID·CSRF·비공개 파일만 기존helper로 revoke하고 사진은 보존한다.

## 4. 나중에 일괄 추출

**Mac/Cloud 작업브랜치 루트**에서 실행한다. AWS SSO와 session-manager-plugin이 필요하다.
App와 PLG의 일회용 loopback HTTP를 SSM 암호화 터널로 가져와 SHA·압축파일 경로를 검증한다.
공개 포트나 새 S3 권한은 추가하지 않는다. 출력 폴더는 매번 새로 만든다.

```bash
python3 tests/load/scripts/fetch-nginx-rehearsal.py --run-id '<실행UUID>' \
  --output '<새 아카이브 폴더>'
```

진행 중이면 수집 종료 후 추출한다. 강제 종료로 result가 없으면 마지막checkpoint를 사용하고 불완전으로 표시한다.
회전은 이전 열린inode의 잔여를 읽고 새 파일로 넘어간다. 미복사/copytruncate가 있으면 불완전 표시한다.
Loki는1000건으로 찬 시간 구간을 나눠 조회한다. 단일시각만으로 한도를 넘으면 해당 건을 건너뛰지 않고 실패한다.
현재 Alloy는 원본msec와 다른 수집시각으로 저장하므로 앞2초/뒤60초를 검색하고 접근로그는 msec로 다시 자른다.
배송 지연 시 `--loki-lookahead-seconds 600`처럼 최대3600초까지 넓혀 재추출한다.
에러로그는 여유 검색 구간도 포함되므로 App 원본 범위와 구분한다. API 성공만으로 로그 완전성을 단정하지 않는다.
CloudWatch는 비어 있는 중간 페이지도 nextToken을 따라가고 timestamp/ingestionTime을 따로 보존한다.

### 정밀 수집기를 안 켰을 때

```bash
python3 tests/load/scripts/fetch-nginx-rehearsal.py --run-id '<새 정리용UUID>' \
  --from '2026-10-24T11:53:36+00:00' --to '2026-10-24T11:55:05+00:00' \
  --output '<새 복구 폴더>'
```

실제 시험 시각으로 바꾼다. 기존 PLG/CloudWatch만 복구하고 hostCaptureAvailable=false를 남긴다.
미수집1초 자료나 당시 설정SHA를 복원했다고 쓰지 않는다. 보존 기간/배송 장애로 사라진 자료까지 보장하지 않는다.

## 5. 오프라인 PNG와 Grafana 캡처

Matplotlib은 로컬 가상환경에만 설치한다.

```bash
python3 -m venv /private/tmp/yeodam-nginx-plot
/private/tmp/yeodam-nginx-plot/bin/pip install -r tests/load/requirements-nginx-rehearsal-plot.txt
/private/tmp/yeodam-nginx-plot/bin/python tests/load/scripts/render-nginx-rehearsal.py --archive '<추출 폴더>'
```

host.png와18개 패널 시계열 PNG/CSV를 만든다. 호스트 자료가 없으면 host.png를 만들지 않는다.
결측을0으로 바꾸지 않고2.5초 초과 표본 간격은 선을 끊는다. analysis.json에 정상probe와 Loki 누락/여분 ID를 기록한다.
series가 없는 그래프는 No stored series이며 오류0의 증거가 아니다. 실제 여정은
`--journey '<k6 결과 폴더>' --audit '<journey-audit.json>'`으로 연결한다. 여행ID와 수집 시간 범위도 검사한다.
추가 그래프 버전은 `--output '<새 그래프 폴더>'`로 원본을 덮지 않고 만든다.

Grafana 실화면은 ego-browser의 로그인된 같은 TaskSpace에서 아래 모듈로 캡처한다.
먼저 `ego-browser skill` 절차로 TaskSpace를 만들거나 재개한다.

```javascript
const {capture}=await import('file://<Cloud절대경로>/tests/load/scripts/capture-nginx-rehearsal.mjs');
await capture({spaceId:24,directory:'<새 PNG 폴더 절대경로>',
  from:Date.parse('<시작UTC>'),to:Date.parse('<종료UTC>'),phase:'historical'});
```

spaceId24는 준비시험의 예시이며 재사용을 전제하지 않는다. phase는before/during/after/historical이다.
스크롤하며13개 그래프의 전체 가시범위를 확인하고 missingGraphPanels를 기록한다.
촬영 시각과 표시 대상 시각을 분리한다. 브라우저가 꺼져 당시 캡처가 없었다면 historical로 남긴다.

## 판정

수집 종료·조회 성공·이미지 저장은 준비 검증이다. 강사 본시험 성공은 정상 로그인/조회,
업로드·AI 완료·DB/S3·연결 부족/5xx/Timeout·종료 후 회복으로 판단한다.
공격용401/429와 정상 사용자 실패를 분리하고 발생기 기록이 없으면 전체 요청 손실률은 미확정으로 둔다.
원본·실패·부분 추출본을 덮지 않고 보존한다.

공식 규격: [Prometheus query_range](https://prometheus.io/docs/prometheus/latest/querying/api/),
[Loki 시간 경계](https://grafana.com/docs/loki/latest/query/logcli/getting-started/).
