# V1 스테이징 조회 제한 Dry Run — #418

> 이 문서는 #418 당시의 관측 모드 기록이다. #420 실제 차단과 현재 renderer 계약은
> [공통 조회 제한](NGINX-READ-ENFORCEMENT.md)을 따른다. 아래 당시 수치·설정은 비교 근거로 보존한다.

정상 브라우저 실측 #416의 타임라인을 근거로 공통 조회 `5r/s`, `burst=30`,
`nodelay` 후보를 관찰한다. **실제 지연·거절을 켜는 도구가 아니다.**
Dry Run에서도 초과량은 계산하지만 요청은 그대로 전달한다.

대상은 Account `483175530259`, 서울 `ap-northeast-2`,
V1 App `i-05d320e3a87200d5a`, 공개 Origin
`https://v1-staging.yeodam-2gether.com`이다. V2 QA와 운영 설정은 바꾸지 않는다.

## 어떤 요청을 묶는가

`map "$request_method:$uri"`로 GET/HEAD `/api/` 요청에만 `$binary_remote_addr`를
부여한다. URI·검색어·Cookie·인증 여부로 bucket을 쪼개지 않는다. 등록되지 않은
조회 경로도 포함한다. 외부 X-Forwarded-For를 제한 키로 신뢰하지 않는다.

빈 키로 제외하는 요청은 다음과 같다.

- GET/HEAD `/api/actuator/health`, `/api/auth/csrf`, `/api/auth/kakao/authorize`,
  `/api/auth/kakao/callback`의 정확한 경로.
- POST/PATCH/PUT/DELETE/OPTIONS와 API 외 HTML·정적 파일·S3 전송.

따라서 기존 multipart 업로드와 수정 요청은 이 조회 bucket을 소비하지 않는다.
인증·쓰기·HTML·전체 TCP 폭주 방어까지 해결했다는 뜻은 아니다.
HTTPS server에 하나의 `limit_req`를 두어 기존 `/api/trips`와 `/api/` location이
같은 zone을 상속한다. 기존 proxy 경로·header·600초 업로드 timeout은 유지한다.
HTTP→HTTPS 리다이렉트 서버와 loopback 상태 서버에는 이 정책을 추가하지 않는다.

```nginx
limit_req_zone $yeodam_read_limit_key zone=yeodam_staging_reads:10m rate=5r/s;
# HTTPS server
limit_req zone=yeodam_staging_reads burst=30 nodelay;
limit_req_dry_run on;
limit_req_status 429;
```

429는 향후 실제 거절 응답 설정이다. 현재 Dry Run은 429를 만들지 않는다.
`limit_req_status=REJECTED_DRY_RUN`과 HTTP status는 별도 값이다.
로그에 `PASSED`, `REJECTED_DRY_RUN`, 빈 값(제외 요청)을 기록하고 기존 Alloy가
같은 Loki stream으로 수집한다. IP·request_id를 새 metric/stream label로 만들지 않는다.

## 적용 도구와 원복

Cloud 작업 공간에서 코드를 검토하고 아래 두 파일을 V1 App의 root 전용
`/opt/yeodam/ops/nginx-read-dry-run/`에 설치한다.

- `nginx_read_dry_run.py`: 고정 Dry Run 패치. 운영 도메인·다른 제한·부분 적용은 거절.
- `install-nginx-read-dry-run.py`: Instance ID·현재 파일 SHA 확인, 백업, syntax 검사·graceful reload.

App SSM에서 실행한다. 먼저 현재 site의 SHA256을 읽고 예상값과 대조한다.

```bash
sudo sha256sum /etc/nginx/sites-enabled/yeodam-v1-staging
sudo python3 /opt/yeodam/ops/nginx-read-dry-run/install-nginx-read-dry-run.py install \
  --expected-site-sha256 '<직전 읽기 전용 점검의 SHA256>'
```

예상 결과는 `changed`, `backup`, `beforeSha256`, `afterSha256` JSON이다.
`nginx -t`가 실패하면 reload하지 않고 원본으로 복구한다. 기존 설정이 예상과 다르면
다시 읽고 차이를 확인하며 예상 Hash만 무조건 교체해 재실행하지 않는다.
원본은 `/var/backups/yeodam/nginx-read-dry-run/apply-*`에 root 전용으로 보존한다.
같은 설정 재적용은 no-op이며 Docker·Alloy·DB를 재시작하지 않는다.
미래 V1 staging renderer도 같은 Dry Run을 생성해 재배포 시 관측이 사라지지 않게 한다.
운영 `nginx/yeodam.conf` 자체는 바꾸지 않는다.

이번 실제 적용 결과:

- before: `ddaf5c4dfdd287be4b9e06d9e4de7352d7a44f468b5bd0f6551210c1d79fc348`
- after: `9c9b764592dcd4fa7a09dce6840efe654197f907e383383b47bd28bfc36b4c09`
- backup: `/var/backups/yeodam/nginx-read-dry-run/apply-_k3vq05h`

문제가 생기면 현재 Hash와 백업의 before/after를 확인한 뒤 원복한다.

```bash
sudo python3 /opt/yeodam/ops/nginx-read-dry-run/install-nginx-read-dry-run.py rollback \
  --backup /var/backups/yeodam/nginx-read-dry-run/apply-_k3vq05h \
  --expected-site-sha256 9c9b764592dcd4fa7a09dce6840efe654197f907e383383b47bd28bfc36b4c09
```

이후 헬스·JSON 파싱·Loki 도착과 원래 Hash를 확인한다. 다른 작업이 site를 변경했다면
자동 원복은 거절한다. 이번 시험에서 원복 명령을 실제 실행하지는 않았다.

## 실제 검증 — 2026-10-24 KST

사용자는 이후 k6 명령 전달 대신 직접 실행을 위임했다. 기존 전용 발생기
`i-0c53b65eb35dd2b42`의 `c6i.large`, k6 `v2.3.0`을 사용했다.
시작 전 실행 중 k6는0개였다. `nginx-dry-run-418` 별도 디렉터리에 원본 시나리오를
설치했고 다른 마이그레이션/기준선 파일은 덮어쓰지 않았다.
HTTP/2.0, 연결 재사용, 자동 retry·redirect 없음, 요청 timeout5초를 고정했다.

| 구간 | 부하 / 응답 | Dry Run 판정 |
| --- | --- | --- |
| 짧은 확인 | 2RPS×5초,10건 모두401 | 주 시험 전 확인, 별도 결과 |
| 초과 요청 | 20RPS×30초,600건 모두401 | PASSED180 / REJECTED_DRY_RUN420 |
| 정상 FE 흐름 | 전·중 각1회, 합성 계정 실제 지도·목록·폴더·사진·원본·검색 | 두 흐름66건 모두200·PASSED |
| 동일 계정3탭 | 각150장 grid 완료, 부하 종료 후 수행 |30건 모두200·PASSED(브라우저27+SSR3) |
| 종료 후 확인 | 홈·상세·13폴더 표시 |7건 모두200·PASSED |

정상 합계103건 모두200·PASSED. k6는 실제600건, dropped0, 예상 밖 응답/프로토콜0,
threshold 모두 통과했다. k6 p95는3.734ms이며 **인증 없는401 응답의 지연**이다.
정상 API 응답이나 사용자 작업 전체 지연과 섞지 않는다.
20RPS는 거절 예정 상태가 생기는지 확인하기 위한 짧은 부하이며 공격 용량의 재현값이 아니다.

주 부하 UTC `2026-10-23T18:19:43.629Z`~`18:20:13.6Z`에 정상 브라우저 흐름
`18:19:45.931Z`~`18:20:00.292Z`를 다른 출처에서 실행했다. 같은 계정3탭 사진 진입은
`18:20:26.245Z`~`18:20:27.343Z`로 **부하 종료 후**다. 이를 부하와 동시에 수행했다고 쓰지 않는다.
기존 S3 사진을 읽었으며 새 업로드·AI 실행은 하지 않았다.

조회 수집 run `26ec989c-7915-42c7-8c5d-39d40f0f4cfe`,
UTC `18:18:22.738Z`~`18:22:47.316Z`의703건은 Loki에도703건 도착해 누락·중복0이었다.
출처는 HMAC으로 익명화했다. 발생기600, 브라우저87, FE 서버 조회16으로 분리된다.
서버 조회16건은 사용자 확인13건·여행 상세3건이며 모두 PASSED다. 여러 실제 사용자의
SSR이 한 출처에 합산되는 조건을 검증한 것은 아니다.
기존 수집 도구의 phase 명칭 single-1/2/3은 각각 전/주 시험·3탭/복구 구간으로 재사용했다.
`all_four_phases_have_requests=false`는 #416의4단계 전체 반복을 수행하지 않았다는 의미다.

헬스·CSRF·실재하지 않는 POST 경로의 제한 상태는 빈 값, HEAD 지도·미등록 GET API는
PASSED였다. POST 안전 확인의403과 인증 없는 HEAD/GET 확인의401은 별도 scope probe다.
이를 정상 조회103건 실패로 세거나 로그 수집 오류로 해석하지 않는다.

70초 관측35회 헬스 모두200, App CPU 최대27.2%, 메모리57.53%, 루트54.66%였다.
Nginx master PID3765968, FE·BE·Alloy 시작 시각 유지와 restartCount0을 확인했다.
이 값은 작은 시험의 관측이며 성능 상한·SLO를 증명하지 않는다.

## Grafana에서 판정 확인

스테이징 Grafana의 **Explore → Loki**, 또는 **V1 staging source**의 기존 Nginx 로그
패널에서 해당 시험 시간을 선택한다. Loki의 stream은 그대로이며 판정 필드를 JSON으로 읽는다.

```logql
{environment="staging",service_name="v1-nginx",stream="access"}
  | json
  | limit_req_status="REJECTED_DRY_RUN"
```

빈 문자열은 조회 제한에서 제외된 요청이고 필드 자체가 없는 과거 로그와 구분한다.
Dry Run 상태의401은 BE 인증 거절이며 방어 차단429가 아니다. `PASSED`가 필요하면 위
필터 값만 바꾼다. 이번에는 새 dashboard나 장애 알림을 추가하지 않았다.

## 시험 세션과 원본 근거

`tests/load/scripts/nginx-fixture-session.py`는 고정된 #416 합성 사용자11007의
기존 email 식별자를 대조한 뒤 새30분 세션1개만 발급/폐기한다. 새 사용자·사진·여행을
만들거나 실사용자의 세션을 읽지 않는다. App root·Instance ID·쓰기 허용 상태·기존
출력 파일 여부를 검사한다. `synthetic-session-pool.py`와 같은 디렉터리에 설치한다.

App에서 `issue --run-id <새 UUID>`로 발급하고, 같은 run ID로 `revoke`한다.
발급 파일은 `/run/yeodam-nginx-read-fixture/<UUID>.json`(600)이다. 키나 토큰을 채팅·
SSM 출력·환경 출력에 복사하지 않는다. 브라우저 사용 시 기존 one-shot loopback 송신과
SSM 암호화 터널로 비공개 임시 파일에만 전달한다. 기존 인증 쿠키가 있으면 덮어쓰지 않는다.
이번 세션은 종료 후 DB SID 잔여0, 원격/로컬 인증 파일 삭제, 정확한 시험 쿠키2개 삭제를
확인했다. 기존 S3 키를 공유하는 fixture 데이터는 삭제하지 않는다.

익명 capture/events/summary, Loki 대조, 자원 관측, 브라우저 시간과 k6 원본은 Cloud 로컬
`.local-docs/evidence/418-nginx-read-dry-run/`에 보존했다. 서버의 원본 로그·HMAC 키와
인증 파일은 Git에 넣지 않는다.

events SHA256: `56bd900c25a194ea743c347d45f5dff937c0381392a3fe7b7f2de6dc97ce4ef1`.
발생기 결과는 `/opt/yeodam-load/results/nginx-dry-run-418/`에 있다.

다음 판단은 후보를 실제 제한으로 전환하고 같은 부하·정상 조회 및 업로드를 비교할지다.
현재는 카운터 판정·정상 수용·관측 경로를 확인했으며 실제 방어 효과, 정상 업로드의
제한 적용 후 보존·AI 완료, 강사 재시험은 아직 수행하지 않았다.

참조: [Nginx 공식 limit_req 문서](https://nginx.org/en/docs/http/ngx_http_limit_req_module.html).
