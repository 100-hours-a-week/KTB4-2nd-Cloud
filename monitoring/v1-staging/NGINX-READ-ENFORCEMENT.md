# V1 스테이징 공통 조회 제한 — #420

#416의 정상 FE 호출 실측과 #418 Dry Run을 거친 `5r/s`, `burst=30`, `nodelay`를
실제 거절 모드로 전환한다. Account `483175530259`, 서울 `ap-northeast-2`,
V1 App `i-05d320e3a87200d5a`, `https://v1-staging.yeodam-2gether.com`만 대상이다.
운영 V1과 V2 QA의 설정·DB·이미지는 이 작업으로 바꾸지 않는다.

## 적용하는 범위

GET/HEAD `/api/`를 실제 Nginx peer 주소별 하나의 bucket으로 묶는다.
헬스, CSRF, OAuth authorize/callback의 정확한 경로와 쓰기·HTML·정적 파일은 빈 키로 제외한다.
외부 X-Forwarded-For를 제한 키로 신뢰하지 않는다. 상세 map은
[Dry Run 문서](NGINX-READ-DRY-RUN.md)에 있고, #420은 속도·burst·경로를 바꾸지 않는다.

```nginx
limit_req_zone $yeodam_read_limit_key zone=yeodam_staging_reads:10m rate=5r/s;
# 기존 HTTPS server에 적용, 기존 API location이 상속
limit_req zone=yeodam_staging_reads burst=30 nodelay;
limit_req_dry_run off;
limit_req_status 429;
```

`burst=30`은 매초 35건 허용이라는 뜻이 아니다. 초과량을 최대30까지 즉시 통과시키고
5r/s로 여유가 회복된다. 이후 초과 요청은 대기 없이429로 끝낸다.
[공식 limit_req 설명](https://nginx.org/en/docs/http/ngx_http_limit_req_module.html)을 따른다.
기존 multipart·업로드600초 timeout·proxy header·라우팅·worker_connections는 유지한다.
요청 빈도 방어이며 유휴 TCP·분산 출처·인증/쓰기 폭주까지 해결했다고 해석하지 않는다.
SSR은 FE 서버가 실제 peer이므로 여러 사용자의 SSR이 합산될 수 있다.

## 적용과 원복

Cloud 작업 공간에서 아래 파일 세 개를 검토하고 App SSM의 root 전용
`/opt/yeodam/ops/nginx-read-enforcement/`에 설치한다.

- `nginx_read_dry_run.py`: 기존 검증된 map·zone·logging 계약.
- `nginx_read_enforcement.py`: 위 계약을 검증하고 거절 모드로 변경.
- `install-nginx-read-enforcement.py`: App Instance ID·기대 SHA·백업·syntax·reload guard.

App SSM에서 현재 SHA와 설정을 먼저 확인한다. 운영 도메인이나 부분 변경을 발견하면 중지한다.

```bash
sudo sha256sum /etc/nginx/sites-enabled/yeodam-v1-staging
sudo python3 /opt/yeodam/ops/nginx-read-enforcement/install-nginx-read-enforcement.py install \
  --expected-site-sha256 '<방금 확인하고 차이를 검토한 SHA256>'
```

성공 JSON의 `backup`, `beforeSha256`, `afterSha256`을 보존한다. `nginx -t` 후 graceful reload하며
컨테이너나 MySQL을 재시작하지 않는다. 검증 실패 시 원본을 복구한다. 이미 같은 설정이면 no-op이다.
다른 제한·변경된 rate/burst·부분 삽입은 자동 덮어쓰지 않는다.

```bash
sudo python3 /opt/yeodam/ops/nginx-read-enforcement/install-nginx-read-enforcement.py rollback \
  --backup '<해당 apply가 출력한 /var/backups/yeodam/nginx-read-enforcement/apply-*>' \
  --expected-site-sha256 '<해당 apply의 afterSha256>'
```

원복 후 SHA와 헬스를 확인한다. 백업·현재 파일이 다르면 자동 원복은 거절한다.
#420은 업로드 시험의500을 발견했을 때 실제로 Dry Run 복구를 실행해 원복 경로도 확인했다.
그500의 원인은 합성 사용자에 누락된 `user_stats` 행으로 확인됐으며 Nginx429가 아니었다.
원인을 보완한10장 시험과 원본 보존 확인 후 차단을 재적용했다. 실패 기록은 별도 보존한다.

## 같은 조건의 비교와 사용자 보호

#418과 같은 k6 v2.3.0, 20RPS·30초·12VU, HTTP/2.0, 연결 재사용, 자동 retry/redirect 없음,
요청 timeout5초를 유지한다. 인증 없는 지도 반복은401 또는 차단429가 예상 응답이다.
429 수만으로 정상 사용자 보호를 판단하지 않는다.

- Nginx `request_id`별 HTTP status·`limit_req_status`·`upstream_status`를 대조한다.
  실제 거절은429/REJECTED이고 upstream은 빈 값이다. upstream 미연결을 빈 값으로 허용한다.
- 다른 출처의 실제 FE 정상 지도·목록·폴더·사진·원본·검색을 전/중/후에 확인한다.
  동일 계정3탭은 여러 독립 사용자 부하를 대신하지 않는다.
- 실제10/150장 baseline 업로드는 임시 쓰기 API가 아니라 원본 보존·실제 AI 완료·최종 조회까지 실행한다.
  현재 시험은 Mac Docker 업로더와 EC2 조회 발생기를 분리한다. #414 EC2 업로드 시간과 직접 성능 비교하지 않는다.
- 사진 수는 DB `trip_attachments`와 S3 HEAD로 대조한다. V1의 `files.original_size_bytes`가 NULL이면
  고정 manifest의 크기 multiset과 S3 ContentLength를 대조하고 DB 크기 미기록을 명시한다.
  이는 객체 존재·개수·크기 검증이며 본문 checksum 비교는 아니다.
- 정상 사용자429/5xx/timeout, 데이터·작업 누락, OOM/restart, 디스크80% 이상, 비회복을 발견하면
  추가 부하를 멈추고 안전 원복한다. 원인 확인 전 큰 시험으로 진행하지 않는다.

## 업로드 시험 세션

업로더는 새 합성 계정1개를 사용한다. `nginx-upload-session.py`를 같은 디렉터리에
`synthetic-session-pool.py`와 함께 설치한다. 기존 migration pool과 실사용 계정은 건드리지 않는다.

```bash
# App SSM, 작업별 새 UUID. JSON은 root 전용 /run에 생성되며 값을 출력하지 않는다.
sudo python3 /opt/yeodam/ops/nginx-read-enforcement/nginx-upload-session.py issue --run-id '<UUID>'
# 30분 세션의 잔여 시간이 부족하면, 실행 중 HTTP 작업이 없는 시점에만 교체한다.
sudo python3 /opt/yeodam/ops/nginx-read-enforcement/nginx-upload-session.py renew --run-id '<같은 UUID>'
# 시험 후 본인이 발급한 SID·CSRF와 비공개 파일만 폐기. 여행·통계·원본은 결과 대조용으로 남긴다.
sudo python3 /opt/yeodam/ops/nginx-read-enforcement/nginx-upload-session.py revoke --run-id '<같은 UUID>'
```

BE 고정 이미지의 회원가입은 `UserStats` 기본 행도 만든다. 이 도구는 정확한
`mig-<run-id>-0000@staging.invalid` 소유자에 한해 누락 행만 기본값으로 추가한다.
기존 통계나30GB quota를 덮어쓰지 않는다. 테스트 데이터가 있는 계정에 기존 pool의 사용자 삭제 cleanup을 실행하지 않는다.
`initialize-stats`는 이미 발급한 동일 소유자 행을 보완할 때만 사용한다.

비공개 pool은 승인된 SSM 암호화 터널로만 업로더에 전달한다. 파일600/디렉터리700을 유지하고
CI artifact·Git·명령행·console에 JWT를 넣지 않는다. `K6_SESSION_POOL_FILE`은 파일 경로만 전달한다.
기존 `K6_ACCESS_TOKEN` 방식도 유지되며, 파일 방식은 V1 staging URL·단일 세션·잔여900초 이상을
HTTP 시작 전에 확인한다. 이900초는 실측 대비 준비 여유이며 토큰 유효기간을 연장하는 설정이 아니다.
Mac 임시 파일도 시험 후 삭제한다.

## 관측과 근거

Grafana **V1 staging source** 또는 Explore → Loki에서 실제 시험 시간을 지정한다.

```logql
{environment="staging",service_name="v1-nginx",stream="access"}
| json
| limit_req_status="REJECTED"
```

동일 stream에서 `status`, `upstream_status`, `request_id`를 함께 확인한다.
request ID·IP를 새 metric label로 만들지 않는다. `nginx-read-demand.py`의 익명화된 조회 기록은
쓰기 POST를 포함하지 않으므로 업로드는 k6 request ID·별도 Nginx 로그·DB/S3로 확인한다.
로컬 실제 결과·원본 증거·SSM Command ID는 `.local-docs/420-nginx-read-enforcement.md`에 기록한다.

### #420 실제 결과 — 2026-10-24

- 같은 20RPS·30초: Dry Run 600건 모두 BE401 → 실제601건 중180건 BE401/421건 Nginx429.
  정상 FE103건은 모두200/PASSED. 같은 계정3탭은 부하 종료 후 확인했다.
- 첫10장500은 합성 사용자 통계 행 누락으로 확인해 실제 원복했다. 보완 후 Dry Run10장 성공과
  S3 원본10개를 확인했다. 이 실패는 차단 실패와 분리해 보존한다.
- 차단 재적용 후150장 업로드·실제 AI 완료·최종 조회 성공(579.984초).
  DB150/S3 원본150,383,664,318B,manifest 크기 multiset 일치. 본문 checksum 비교는 하지 않았다.
  조회 반복600건을 업로드 초반30초만 겹쳤고180건 BE401/420건 Nginx429였다.
- Loki 대조는 첫708/708·두 번째602/602로 누락·중복0. 컨테이너 재시작0/공개 헬스200,
  실제 차단 SHA `57dcf4d79f853496791c7355d29a4d272fd8871d9231952a23ab046668925096` 유지.
- 업로드와 겹친 초반70초에서 App CPU는 최대100%였다. 이 결과를 CPU 여유·전체 공격 방어로
  해석하지 않는다. 같은 NAT/SSR 다사용자 합산과 TCP 연결 고갈 방어는 추가 근거가 필요하다.
- 합성 세션·CSRF·Mac 비공개 파일은 폐기했다. 로컬 Cloud Release configuration 검증은 통과했고,
  원격 PR Gate와 리뷰1명은 Push 후 확인한다.
