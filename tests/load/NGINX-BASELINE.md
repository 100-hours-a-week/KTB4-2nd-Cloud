# Nginx 방어 전 정상 기준선 — #414

방어 설정이 없는 상태에서 정상 요청의 연결·지연·오류와 업로드 완료를 기록한다.
현재 관측 설정은 #410 JSON 로그 회전, #412 연결 지표·로그 필드를 포함한다.
9/29와 동일한 이미지/발생 패턴을 재현하는 시험은 아니다.

## 실행 조건

- 대상: `https://v1-staging.yeodam-2gether.com`만 사용한다.
- V1 App은 기존 FE·BE와 ProxySQL 경유 DB를 유지한다.
- 기존 V1 CPU Worker와 c6i.large 발생기를 기동한다. V2 QA·운영·공유 GPU를 변경하지 않는다.
- 사진10장→완료 여행 조회→11장→150장 순서로 각각 실행하고, 결과를 확인한 뒤 다음 단계로 간다.
- 업로드는1 VU·여행1개, 조회는1 VU·5분·요청 사이2초다. 최대 처리량 측정이 아니다.
- OAuth 로그인은 브라우저에서 직접 한다. k6는 로그인 후 Access Token을 이용한 인증 확인부터 시작한다.
- Token은 숨김 입력하고 채팅·인자·Git·결과 파일에 넣지 않는다. 각 단계 시작 전에 새 Token을 준비한다.
- 정확한 실행 이미지·Nginx 설정 hash·Manifest hash와 결과는 로컬 기록에 보관한다.

## 기동·준비 현황

2026-10-23 확인: App55%/MySQL10%, 발생기40%, Worker46% disk 사용.
Worker `/health`200·model_loaded true·active_tasks0·queued0이며 App에서도 사설 호출200.
`/ready`404는 이 V1 이미지의 엔드포인트 부재이며 readiness 실패로 해석하지 않았다.

발생기는 k6 v2.3.0이다. Manifest의 모든 실제 파일 존재와 sizeBytes 일치를 확인했다.

| 사진 수 | Byte | 최대10장 Batch Byte |
| --- | --- | --- |
| 10 | 24,645,535 | 24,645,535 |
| 11 | 27,520,695 | 24,645,535 |
| 150 | 383,664,318 | 29,661,949 |

해당 크기 확인은 파일 내용이나 AI 결과의 정확성 검증이 아니다.
10/11장 시험으로 Batch 경계를,150장 시험으로 장시간 작업을 확인한다.

## 첫 실행:10장

**Mac 터미널**에서 발생기로 접속한다.

```bash
aws ssm start-session --profile yeodam-admin --region ap-northeast-2 \
  --target i-0c53b65eb35dd2b42
```

**SSM 세션**에서 아래 명령을 입력하고 ubuntu 프롬프트로 바뀐 뒤 다음 블록을 입력한다.

```bash
sudo -iu ubuntu
```

별도 디렉터리에 고정한 스크립트를 사용한다. 기존 마이그레이션 source/results는 수정하지 않는다.

```bash
cd /opt/yeodam-load/nginx-baseline-414/tests/load
export K6_BASE_URL='https://v1-staging.yeodam-2gether.com'
export K6_DATA_MANIFEST='/opt/yeodam-load/manifests/baseline-10.json'
export K6_RESULTS_DIRECTORY='/opt/yeodam-load/results/nginx-baseline-414'
export K6_TRIP_NAME_PREFIX='nginx414-10'
read -r -s -p 'V1 accessToken: ' K6_ACCESS_TOKEN
echo
export K6_ACCESS_TOKEN
./scripts/run-k6.sh scenarios/upload-baseline.js
```

V1 스테이징 로그인 후 브라우저 개발자 도구 Application/Storage→Cookies에서
`accessToken` 값만 복사한다. `Bearer `나 Cookie 이름은 붙이지 않는다.
시험 종료 후 Token을 제거한다.

```bash
unset K6_ACCESS_TOKEN
```

인증401이면 재로그인하고 새 Token을 입력하며 부하를 올리지 않는다.
실패 때 재실행하기 전에 생성된 trip_id·서버 작업 상태를 먼저 확인한다.

## Grafana와 결과

https://grafana-staging.yeodam-2gether.com/d/yeodam-v1-source

최근15분·15초 Refresh로 설정하고 시작/종료 시각을 기록한다.

- Client 연결: Active/Reading/Writing/Waiting과 accepted/handled rate
- 공개 요청: RPS·5xx·request p95, 업로드 p95와 오류 로그
- 자원: Host 메모리·CPU·Root disk
- 로그: 생성·업로드 request_id와 connection_requests

접근 로그 p95는 시간창·경로별 통계다. 소량의 마지막 업로드 한 건을 p95로만
해석하지 않고 개별 요청 시간도 기록한다. 상태 페이지 수집 자체도 연결 카운터에
포함된다. 그래프 No data를 오류0으로 해석하지 않는다.

사용자는 결과 디렉터리와 완료/실패 출력을 전달한다. Cloud 담당은 읽기 전용으로
summary·run.log, 지정 시간창 로그와 DB/S3 결과를 대조한다. 원본10/10·AI 완료·조회가
모두 맞는지 확인한 뒤 그 여행을 조회 기준선의 대상으로 사용한다.

## 다음 단계와 중단

조회는 `view-baseline.js`에 해당 계정 소유의 완료 trip_id를 지정하고1 VU/5분/2초로
수행한다.11장과150장은 각각 새 여행1개로 수행한다. 각 명령은 직전 결과를 확인한 뒤
안내한다.150장은 기존 처리시간이 길었던 점을 감안해 k6·Nginx Timeout과 실제 완료를
먼저 대조하며, 통과시키기 위해 Timeout을 임의로 늘리지 않는다. Token 유효기간과
발생기 메모리도 단계 시작 전에 다시 확인한다.

오류·사진 수 불일치·OOM/재시작·디스크80% 이상·연결 부족 오류가 나오면 중단하고
원인을 기록한다. 마지막 업로드 응답과 실제 AI 완료가 다르면 별도로 판정한다.

아래 결과는 정상 조건의 소량 시험이다. 방어 효과·최대 RPS의 증거는 아니다.
시험 종료 후 결과를 보존하고 Worker·발생기를 기존 비용 정책에 맞춰 중지한다.


## 실행 결과 — 2026-10-23

| 시험 | 결과 | 핵심 측정 |
| --- | --- | --- |
| 10장 업로드 | 여정 성공 | 42.83초, HTTP6건 실패0, DB완료·S3 10/10 |
| 완료 여행 조회1 VU·5분 | 성공 | 상세149회, p95 29.42ms, HTTP150건 실패0 |
| 11장 업로드 | 여정 성공 | 40.39초, 중간204·마지막200, DB완료·S3 11/11 |
| 150장 업로드 후 조회 | 전체 여정 실패 | 8분58.71초, 업로드15개 Batch 통과·AI완료·S3 150/150, 마지막 상세401 |
| 150장 기존 여행 복구 조회 | 별도 확인 | 새 인증으로1회 성공·31.54ms. 원래 실패와 분리 |

S3는 HeadObject의 존재·총 Byte를 Manifest와 대조했다. 전체 객체 내용의 Checksum
동일성을 검증한 것은 아니다. 한 번의 업로드 시간을 대표 p95나 개선율로 주장하지 않는다.

150장 실행의 토큰 발급은12:35:08Z·만료13:05:08Z였고 실패 조회는13:09:49Z였다.
시작 시 남은 수명은 약4분17초였다. 기존 여정의실패를유지하고, 새인증의복구조회와
분리한다. 사진을재업로드해성공값으로대체하지않았다.

### 다음 장시간 실행 전 토큰 수명 확인

새로고침만으로 Access Token이 재발급됐다고 가정하지 않는다. V1에서 다시 로그인한
토큰을 숨김 입력하고 다음 검사를 통과한 경우에만 장시간 시험을 실행한다.
아래15분은 측정된150장 여정 약9분에 여유약6분을 더한 이번 시험의 준비 기준이다.
모든 부하에 적용되는 Token TTL이나 완료시간 보장은 아니다.

```bash
python3 scripts/check-access-token-window.py --min-remaining-seconds 900 && \
  ./scripts/run-k6.sh scenarios/upload-baseline.js
```

이 도구는 로컬 JWT의iat/exp만 해석하며 Token·사용자·세션 Claim을 출력하지 않는다.
서명을 검증하는 도구는 아니므로 실제 인증 성공은 기존 `/api/users/me` 검사로 확인한다.
수명이 부족하거나 만료되면 k6를 시작하지 않는다. BE TTL을 늘리거나401을 성공으로
분류하는 방식으로 통과시키지 않는다.
