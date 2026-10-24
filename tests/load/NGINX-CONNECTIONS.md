# V1 스테이징 연결 유지·교체 시험 — #422

#420의 실제 조회 제한(5r/s·burst30·nodelay·초과429)을 유지한 채 연결 동작을 측정한다.
대상은 Account483175530259, 서울ap-northeast-2의 V1 App i-05d320e3a87200d5a와
기존 c6i.large 발생기 i-0c53b65eb35dd2b42다. 공개 Origin은
`https://v1-staging.yeodam-2gether.com`이다. Keepalive·연결 한도·이미지·DB 연결은 바꾸지 않는다.

## 먼저 확인한 실행 조건

- Nginx1.24, worker2개·worker_connections768, worker FD soft1024/hard524288.
- 현재 `keepalive_timeout` 명시값 없음. 기본75초이므로 실제 EOF를 확인할 관측 시간이 필요하다.
  [공식 Keepalive 설명](https://nginx.org/en/docs/http/ngx_http_core_module.html#keepalive_timeout).
- 기존 유휴 도구의60초 상한만으로는75초 종료를 볼 수 없어 **도구 상한만90초**로 확장했다.
  연결 수 상한64·요청당 timeout·정확한 도메인 확인·기본 계획 모드는 유지한다.
- V1 staging site SHA는 `57dcf4d79f853496791c7355d29a4d272fd8871d9231952a23ab046668925096`이다.
  다른 설정이면 관측 도구가 시작을 거절한다. SHA만 바꿔 우회하지 말고 차이를 검토한다.

## 시험을 나누는 이유

| 구간 | 조건 | 확인할 것 |
| --- | --- | --- |
| idle8 | HTTP/1.1,8개,1초 간격,최대90초 대기,자동 재접속 없음 | 응답 뒤 유지와 서버의 실제 종료 시간 |
| idle64 | HTTP/1.1,64개,0.05초 간격,최대90초 대기,자동 재접속 없음 | burst 초과 상태·Waiting·서버 종료와 정상 조회 영향 |
| reuse20 | HTTP/2,20RPS·30초,12VU,재사용 켬 | 같은 요청량의 재사용 기준 |
| fresh20 | HTTP/2,20RPS·30초,12VU,재사용 끔 | 새 연결 반복 생성의 영향 |
| fresh50 | HTTP/2,50RPS·30초,12VU,재사용 끔 | 앞 단계 정상 확인 후 한 단계 증가 |

유휴 도구는 완전한 GET과 응답을 처리한 후 기다린다. 미완성 Header·Slowloris 시험이 아니다.
429에서 서버가 바로 연결을 닫는 경우도 정상 관찰로 기록한다. EOF 뒤 다시 열지 않는다.
64연결을 과거 약1,500연결 고갈의 재현으로 해석하지 않는다.

k6의 `noConnectionReuse`는 Keepalive 재사용을 끄는 옵션이다.
[공식 옵션](https://grafana.com/docs/k6/latest/using-k6/k6-options/reference/#no-connection-reuse).
실제 연결이 바뀌었는지는 서버의 connection·connection_requests·accept 카운터로 별도 확인한다.
HTTP 프로토콜이 지정값과 다르거나 예상 밖 상태/timeout이 나오면 k6는 중단한다.
VU12를 TCP12개라고 가정하지 않는다.

## 인증 조회와 자원 관측

`nginx-connection-observer.py`는 App root에서만 실행한다. 기존 합성 read fixture11007의
짧은 세션만 사용하며 `/api/trips/map`, `/api/trips`, `/api/trips/2048046`을 번갈아2초에1회 조회한다.
App에서 공개 HTTPS를 호출하므로 발생기와 출처가 다르다. 직접 BE localhost를 호출하지 않는다.
이는 인증된 API 기능 확인이며 실제 FE 화면·새 업로드·AI 여정 재검증은 아니다.

같은 프로세스가 1초 간격으로 loopback stub_status와 HTTPS Established를 수집한다.
App CPU·디스크와 정상 응답 상태·지연·request ID도 저장한다. 출처 자체에 관측 비용이 포함된다.
시작 직후 첫 CPU 구간은1초보다 짧아 peak 집계에서 제외한다. 정상 지연은 이 Python 클라이언트의
TLS 준비·연결·응답 읽기를 포함하며 k6 HTTP duration이나 이전 브라우저 수치와 직접 비교하지 않는다.
정상 probe가 매번 새 연결을 열므로 accept 전체 증가에는 이 요청과 stub scrape도 포함된다.
부하 출처의 connection ID 대조와 전체 카운터를 함께 봐야 한다.

설치 위치는 `/opt/yeodam/ops/nginx-connections-422/`이며 기존 fixture session helper와
synthetic-session-pool.py를 함께 둔다. 세션은 서버 밖으로 복사하지 않는다.

```bash
# V1 App SSM, root. 각 실행에는 새로운 UUID를 사용한다.
sudo python3 /opt/yeodam/ops/nginx-connections-422/nginx-fixture-session.py issue --run-id '<세션 UUID>'
sudo python3 /opt/yeodam/ops/nginx-connections-422/nginx-connection-observer.py \
  --run-id '<관측 UUID>' --seconds 110 --session-run-id '<세션 UUID>'
# 시험 후 자신이 발급한 SID·CSRF와 비공개 파일만 폐기한다.
sudo python3 /opt/yeodam/ops/nginx-connections-422/nginx-fixture-session.py revoke --run-id '<세션 UUID>'
```

결과는 root 전용 `/var/lib/yeodam/nginx-connections-422/<관측 UUID>/samples.jsonl`과 summary.json이다.
관측 실행과 별도 SSM에서 발생기 도구를 실행한다. 관측 시작5초 후 부하를 시작하고,
관측·발생기 중 하나가 실패하면 다른 명령을 중단한다. 각 단계 결과를 확인하기 전 다음 단계로 가지 않는다.
SSM executionTimeout과 외부 단계 deadline도 지정해 장시간 고아 실행을 남기지 않는다.

발생기의 `tests/load` 작업 위치에서 유휴 도구를 실행하는 예시다.

```bash
python3 scripts/nginx-idle-probe.py \
  --target https://v1-staging.yeodam-2gether.com --run-id nginx422-idle8 \
  --connections 8 --hold-seconds 90 --open-interval 1 --request-timeout 5 \
  --execute --confirm-target https://v1-staging.yeodam-2gether.com \
  --output '<존재하지 않는 결과 JSON 경로>'
```

429/401은 인증 없는 부하에서만 예상 응답이다. 정상 인증 probe에서200 이외 응답은 실패다.
정상 실패·Nginx 연결 부족·OOM/restart·디스크80%·부하 종료 후 비회복이면 확대를 멈춘다.
이 작업은 서버 방어 설정을 변경하지 않아 부하 중단이 첫 복구 조치다.
후속 설정 변경이 필요하면 측정 근거와 사용자 영향부터 별도 작업으로 정한다.

로컬 확인:

```bash
python3 -m unittest discover -s tests/load/tests -p 'test_nginx_defense.py'
python3 -m unittest discover -s tests/load/tests -p 'test_nginx_connection_observer.py'
bash scripts/validate-cloud-pr.sh
```

실제 결과와 원본·시각·명령 ID는 `.local-docs/422-nginx-connections.md`에 별도 기록한다.

## 실제 결과 — 2026-10-24

| 시험 | 실제 요청 | Nginx 고유 연결 | 인증 조회 |
| --- | ---: | ---: | --- |
| HTTP/1.1 유휴8개 |8|8|55건 모두200|
| HTTP/1.1 유휴64개 |64|64|53건 모두200|
| HTTP/2 재사용20RPS |601|12|28건 모두200|
| HTTP/2 새 연결20RPS |601|601|28건 모두200|
| HTTP/2 새 연결50RPS |1501|1501|28건 모두200|

유휴64개 중401은46개,429는18개였다. **429로 차단된 연결도 약75초 유지**됐다.
해당429는 upstream이 빈 값이라 BE에는 도달하지 않았다. 요청 제한과 유휴 연결 점유가 별개임을 확인했다.
다음 판단은 Keepalive 유휴시간 단축의 연결 점유 감소·재접속 비용·정상 여정 영향 비교다.
`limit_conn`은 요청 처리 중 연결만 계수하므로 유휴 연결을 막는 설정으로 바로 추가하지 않는다.
[공식 계수 범위](https://nginx.org/en/docs/http/ngx_http_limit_conn_module.html).

세 k6 구간은 dropped0·threshold 통과. 정상 인증 조회는 총192건 모두200이었다.
새 연결50RPS의1초 Established 표본 최대0은 연결 미생성을 뜻하지 않는다. 실제1501개 연결이
샘플 사이에 열리고 닫혔으며 connection ID·accept 누적으로 확인했다.
평균 TLS handshake는 새 연결20/50RPS에서 각각5.442/5.516ms였다.
`http_req_duration`은 초기 접속 준비 시간을 포함하지 않으므로 TLS 지표와 구분한다.
[k6 지표 정의](https://grafana.com/docs/k6/latest/using-k6/metrics/reference/).

로그2967건 모두 Loki 도착, 누락·중복0. 시험 시간창에서 점검한 로그에 연결 부족·FD 부족·upstream timeout은 없었다.
종료 후 HTTPS Established0·Waiting1로 회복, 헬스200·컨테이너 재시작0·site SHA 유지.
시험 SID·CSRF·비공개 파일은 폐기했다. 새로운 사진 업로드나 AI 여정을 이 연결 부하에서
재실행한 결과는 아니며, 과거 약1500개 동시 연결 고갈의 재현으로 주장하지 않는다.
