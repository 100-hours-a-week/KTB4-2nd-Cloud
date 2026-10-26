# V1 스테이징 Keepalive 유휴시간 비교 — #424

조회 제한을 통과하지 못한 429 응답도 기존 시험에서 약75초 동안 연결을 유지했다.
이번 작업은 HTTPS 서버의 `keepalive_timeout`만 비교한다. 기존 5r/s·burst30·nodelay와
429 응답, worker_connections, 업로드·upstream timeout은 유지한다.

## 후보를 정한 근거

#416의 정상 FE 요청144건을 실제 Nginx 연결별로 대조했다. 브라우저123건·4개 연결의
연속 요청 간격119개에서 p95는13.107초였다. 15초 초과는5개, 30초 초과는4개였다.
SSR21건·10개 연결의 간격11개는 최대4.52초였다. 따라서15초와 더 보수적인30초를
기존75초와 비교했다. 작은 단일 사용자 표본이므로 최적값이나 전체 사용자 보장은 아니다.

브라우저 표본은 HTTP/2였고 이번 연결 실험은 HTTP/1.1이다. 간격 표본을 후보 선정에
활용하되 HTTP/2 브라우저 동작까지 똑같이 재현했다고 해석하지 않는다.

## 실행 경계와 순서

대상은 Account483175530259 / ap-northeast-2의 V1 App `i-05d320e3a87200d5a`와 기존
발생기 `i-0c53b65eb35dd2b42`다. 운영·V2 QA에는 적용하지 않는다.

1. App SSM에서 현재 사이트 SHA, 공개 헬스, 실제 Nginx 설정과 디스크를 확인한다.
2. 별도 작업 디렉터리에 이 폴더의 `nginx_read_dry_run.py`, `nginx_read_enforcement.py`,
   `nginx_keepalive.py`, `install-nginx-keepalive.py`를 함께 둔다.
3. 75초 기준선을 측정한 뒤 현재 SHA를 지정해30초, 다시 확인 후15초를 적용한다.
4. 각 후보에서 동일한64개 연결과 정상 재조회 결과를 확인한다. 실패하면 확대를 멈추고
   현재 SHA를 확인해75초로 원복한다.
5. 최종 후보에서 신선한 합성 세션으로 실제150장 업로드·AI 완료·최종 조회와 DB/S3를 확인한다.
6. 성공한 후보만 renderer에 고정하고, 발급한 SID·CSRF와 임시 인증 파일을 폐기한다.

적용 명령은 **V1 App SSM의 root 권한**으로 실행한다. SHA에는 직전에 읽은 실제 값을 넣는다.
복사한 모듈은 `/opt/yeodam/ops/nginx-keepalive-424/`에 함께 있어야 한다.

```bash
sudo sha256sum /etc/nginx/sites-enabled/yeodam-v1-staging
sudo python3 /opt/yeodam/ops/nginx-keepalive-424/install-nginx-keepalive.py install \
  --seconds 30 --expected-site-sha256 '<현재 사이트 SHA256>'
curl --fail https://v1-staging.yeodam-2gether.com/api/actuator/health
```

예상 출력은 `changed`, 백업 경로, before/after SHA, `keepaliveSeconds`다.
실제 Instance ID·기존 enforcement 형식·현재 SHA가 다르면 중단한다.
`nginx -t` 후 graceful reload하며 실패 시 원본 복구와 재검증을 수행한다.
무조건 restart하거나 다른 변경을 덮어쓰지 않는다.

자신의 timeout 블록을 제거해 원래의 암묵적75초로 돌아가는 명령이다.

```bash
sudo python3 /opt/yeodam/ops/nginx-keepalive-424/install-nginx-keepalive.py install \
  --seconds 75 --expected-site-sha256 '<현재 사이트 SHA256>'
```

또는 `rollback --backup '<원래 백업 경로>' --expected-site-sha256 '<현재 SHA>'`를 사용한다.
백업의 적용 후 SHA와 현재 파일이 같아야 한다. 30→15처럼 여러 번 적용했다면 첫 번째
백업을 무작정 덮어쓰지 않는다. 백업은 `/var/backups/yeodam/nginx-keepalive/apply-*`에 있다.

## 동일하게 비교한 부하

- 발생기: `/api/trips/map`에 HTTP/1.1 연결64개를0.05초 간격으로 연다. 각 응답을 완전히
  읽은 후 최대90초 EOF를 기다린다. 재접속하지 않으며 미완성 헤더 공격은 아니다.
- 정상 조회: App에서 공개 HTTPS를 경유해 지도·목록·상세를2초에1회 번갈아 호출한다.
  부하 출처와 다른 peer인 것을 로그에서 확인한다.
- 정상 재사용: `nginx-keepalive-reconnect.py`가 같은 합성 계정11007의 Requests Session3개로
  각각 지도·목록·상세를 호출하고2·8·20·40초씩 기다린 뒤 재조회한다. 총15건이다.
  `requests==2.31.0`인 기존 Host 환경을 사용했다. 서로 다른 사용자3명·실제 UI 시험은 아니다.
- App은1초 stub_status·Established·CPU·디스크를 기록한다. 75초 기준은110초, 나머지는90초
  관측했다. 정상 실패, 디스크80%, 외부 deadline 또는 프로세스 실패 시 중단한다.
- 재사용 요청은 비200 또는 총 클라이언트 지연1초 초과 시 실패한다. 1초는 기존 시험에서
  가져온 초기 안전 경계이며 팀 SLO가 아니다. 요청별 애플리케이션 재시도는 설정하지 않는다.
  끊어진 idle pool 연결을 라이브러리가 교체하는 정상 동작과 재전송을 혼동하지 않는다.

observer에는 `--experiment 424 --expected-site-sha256 '<현재 SHA>'`를 추가한다.
재사용 도구는 App root에서 다음처럼 실행한다. 두 도구의 세션 파일은 서버 밖으로 출력하지 않는다.

```bash
sudo python3 /opt/yeodam/ops/nginx-keepalive-424/nginx-keepalive-reconnect.py \
  --run-id '<새 UUID>' --session-run-id '<현재 read fixture 세션 UUID>' \
  --expected-site-sha256 '<현재 사이트 SHA256>'
```

`localPort`와 Nginx `connection`을 요청ID로 대조해 재사용·교체를 판별한다.
총 지연에는 DNS·새 TCP/TLS·응답 읽기가 포함된다. TLS 시간만 분리한 값이나 k6의
`http_req_duration`과 같은 값이 아니다. 결과는 root 전용
`/var/lib/yeodam/nginx-keepalive-424/reconnect/<UUID>/result.json`에 남는다.

## 2026-10-24 비교 결과

| timeout | 64개 연결 유휴시간 중앙값 | 정상 인증 조회 | 재사용 시험의 고유 연결 | 20초 후 조회 중앙값 | 40초 후 조회 중앙값 |
| --- | ---: | --- | ---: | ---: | ---: |
|75초|75.000초|70/70 성공|3|26.499ms|17.395ms|
|30초|30.000초|60/60 성공|6|41.057ms|80.047ms|
|15초|15.000초|60/60 성공|9|82.905ms|87.897ms|

각 조건의 부하 응답은401:46·429:18로 같았고,429는 upstream 미연결이었다.
정상190건은 모두200이었다. 재사용 시험의 지연 중앙값은 각 간격당 요청3개 표본이다.
Nginx 로그386건=Loki386건, 누락·중복0이다.

64개 연결의 관측 유휴시간 합은 약4800→1920→960 connection-seconds로 줄었다.
15초에서 약80% 줄었다는 주장은 이 **유휴시간**에 한정한다. Waiting 최대68은 모두 같았다.
1초 CPU 최대는41.71/42.93/54.46%였다. 고정 순서1회 시험으로 CPU 효과·최대 처리량·
고갈 방지 성공을 주장하지 않는다. HTTP/2·분산 출처·장시간 지속 공격은 별도 범위다.

15초에서 실제150장 여정이579.082초에 성공했다. DB COMPLETED/150건·S3원본150개/383,664,318B와 Manifest 크기 다중집합이 일치했다. 마지막 활성 요청은243.341초 후200이었다. 원본 checksum은 비교하지 않았다. 이 결과로 스테이징15초를 채택해 renderer에 반영했다. 운영 적용과 강사 재부하는 별도다.

전체 결과와 원본 근거는 `.local-docs/424-nginx-keepalive.md`에 남긴다.
공식 동작 설명: [Nginx keepalive_timeout](https://nginx.org/en/docs/http/ngx_http_core_module.html#keepalive_timeout).
