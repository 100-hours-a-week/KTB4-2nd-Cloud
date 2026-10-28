# 동일 IP의 조회 부하와 정상 계정 영향 — #456

기존 강사 시험 로그에는 부하/정상 요청의 식별자가 없어, 목록429 중 정상 사용자의
요청을 분리할 수 없었다. 이 시험은 기존 Nginx 제한·인증·라우팅을 유지하고 역할별 요청 ID를 기록한다.
휴대폰·OAuth 조작·브라우저 화면 검증은 필요하지 않다. 과거 신고의 원인을 소급 확정하지 않는다.

## 고정 조건

대상은 기존 V1 `https://v1-staging.yeodam-2gether.com/api/trips`뿐이다.
DB 복제 시험의 Source로 연결되는 DNS/hosts override는 거절한다.
기존 발생기 `i-0c53b65eb35dd2b42`에서 무인증 조회20RPS와 합성 계정 조회1RPS를
동시에 실행한다. App `i-05d320e3a87200d5a`에서는 별도 세션으로 인증 조회1RPS를 실행한다.
같은/다른 출처라는 가정은 실제 Nginx 응답 ID를 단일 익명화 access에 연결해 검증한다.

| 공통 UTC 시작 후 | 발생기 | App 대조군 |
| --- | --- | --- |
| 0~25초 | 인증된 정상 조회1RPS | 인증된 정상 조회1RPS |
| 25~55초 | 정상1RPS + 무인증 목록20RPS | 정상1RPS |
| 55~90초 | 정상1RPS, 회복 관측 | 정상1RPS |

무인증 부하는 인증 세션을 추가로 전송하지 않으면서 같은 IP의 조회 예산을 소모하는
실험 조건이다. 401/429는 이 부하에만 예상 응답이며, 정상 계정의429는 피해로 센다.
HTTP/1.1 연결을 재사용한다. 실제 강사 HTTP/2 전체 패턴의 재현이나 TCP 최대 용량 시험은 아니다.
정상 요청은 부하와 독립된 스레드에서 매초1회 예약하며, 초 시작 후25/75/125/175ms를
고정 순환한다. 정상 요청을 항상 부하보다 먼저 처리하도록 만드는 예약 순서를 피하고
서로 다른 도착 위상을 포함하기 위한 조건이며, 최대1회/초는 유지한다. 같은 값으로 대조군도 예약한다.
부하 동시 작업은4개, 응답 timeout2초, 본문2MB 상한이다. 부족한 슬롯·스케줄 지연은
숨겨서 재시도하지 않는다. HTTP 리다이렉트·자동 재시도·사진 업로드·여행 생성은 없다.

## 실행 준비와 명령 위치

Cloud에서 `aws sts get-caller-identity --profile yeodam-admin --region ap-northeast-2`로
계정483175530259를 확인한다. 기존 발생기의 k6 등 다른 도구와 원래 Nginx의 부하가
없는 시간을 확보한다. 도구도 시작 전과 매초 k6/h2load/wrk/vegeta를 발견하면 중단한다.
다른 채팅의 프로세스를 종료하거나 Nginx·DB·hosts 설정을 바꾸지 않는다.

App의 기존 `nginx-fixture-session.py`로 시험용 Run UUID 두 개에 각각 단일 세션을 발급한다.
고정 합성 계정11007의 서로 다른 세션이며 실제 사용자의 세션을 사용하지 않는다.
하나는 App에 유지하고 다른 하나만 기존 `private-pool-transfer.py`와 SSM 암호화 터널로
발생기에 전달한다. JWT 비밀키는 App을 떠나지 않는다. pool 파일은0700/0600으로 유지한다.
이동한 세션은 이90초 시험에만 사용하고 원본/수신 사본과 자기 SID를 종료 후 폐기한다.

`nginx-shared-ip-trial.py`를 두 인스턴스의 격리된
`/opt/yeodam/ops/nginx-shared-ip456/`에 설치하고 SHA를 대조한다. 기존1초 host/ENA 수집기를
별도 bounded Run으로 시작하고 checkpoint가 최신인지 확인한다. 두 역할에는 같은 시험
Run ID와5~60초 뒤의 공통 UTC 시작 시각을 전달한다. 다음 명령은 각 서버 SSM root에서 실행한다.

```bash
python3 /opt/yeodam/ops/nginx-shared-ip456/nginx-shared-ip-trial.py run \
  --role generator --run-id '<공통 시험 UUID>' \
  --session-run-id '<발생기 세션 UUID>' --pool-file '<발생기 private pool 경로>' \
  --start-at '<공통 UTC ISO 시각>' --execute
```

App에서는 같은 명령의 role을 `control`로 하고 App용 세션 UUID와 pool 경로를 넣는다.
파일 경로만 전달하며 토큰 값을 CLI·SSM 출력·채팅에 넣지 않는다.
출력은 `/var/lib/yeodam/nginx-shared-ip456/<시험 UUID>/<역할>/`의
plan/preflight/requests/checkpoint/summary다. 원시 응답 본문·쿠키는 저장하지 않는다.

## 중단·회복·판정

정상 기준선이200이 아니거나 다른 부하 도구가 실행되면 본부하로 진행하지 않는다.
5xx·통신 오류·1초 초과·스케줄 누락은 중단 사유다. 대조군 실패도 부하 종료를 요청한다.
역할 출력 폴더의 `STOP` 파일로 해당 역할을 멈출 수 있으며, 중단은 완전한90초 결과로 세지 않는다.
발생기 정상 계정의 shared-load429와 종료 후 처음10초의429는 숨기지 않고 결과로 기록한다.
회복 마지막25초는 모두200이어야 한다. 초과한 정상429를 성공으로 세지 않는다.

중단 여부와 관계없이 자기 합성 세션 두 개를 발급 도구로 revoke하고, 발생기 사본을
폐기한 뒤 SID 부재와 private 파일 부재를 확인한다. 원래 사용자·사진·여행은 삭제하지 않는다.

App/Loki를 기존 archive 도구로 보존하고 같은 시간의 요청 ID 다중집합을 대조한다.
두 역할의 redacted requests와 하나의 redacted access를 로컬에서 분석한다.

```bash
python3 tests/load/scripts/nginx-shared-ip-trial.py analyze \
  --generator '<발생기 requests.jsonl>' --control '<App requests.jsonl>' \
  --access '<단일 archive의 access.jsonl>' --probe-map '<App의 고유 표식 대조 JSONL>' \
  --output '<새 결과 JSON>'
```

완전한90초 일정·요청 ID/상태/경로 일치·같은 출처 부하/정상·다른 출처 대조군을 모두 확인한다.
429 응답에는 X-Request-ID가 없을 수 있다. 각 요청은 기존 로그 필드인
X-Migration-Run-Id에 고유 UUID를 보내고, 임시 관측 필드 trial_probe_id→request_id를
비밀값 없는 sidecar로 내보낸다. 단일 표식이 단일 서버 ID에 연결돼야 하며 응답 ID가
있는 경우 둘도 같아야 한다. IP·시각만으로 429 요청을 맞춘 것으로 처리하지 않는다.
그 상태에서 정상 계정의429/REJECTED·upstream미전달과 독립 정상200·회복200을 대조한다.
이 조건에서만 이번 실험의 공유 IP 피해를 입증한다. 실제 앱의 브라우저 재시도나
강사 시험의 어느429가 정상 사용자였는지는 이 시험으로 확정하지 않는다.


### 임시 관측 필드

기존 migration_run_id는 특정 POST 시험 API에만 기록되므로 목록 GET에는 적용되지 않는다.
`monitoring/v1-staging/nginx_shared_ip_observation.py`는 정확한 GET /api/trips의 UUID 형식
헤더만 trial_probe_id로 기록한다. 인증·임의 헤더·쿠키를 기록하지 않는다. 기존 JSON escape를
유지하고 Nginx -t·graceful reload를 거친다. 원본 백업·전후 SHA를 저장하고 종료 시
rollback --run-id <UUID> --expected-site-sha256 <관측 적용 SHA>로 원래 SHA를 복원한다.
다른 편집으로 SHA가 달라지면 덮어쓰지 않는다. 읽기5r/s·burst30/생성1r/s·burst5/TCP
보호는 변경하지 않으며 관측 필드 유무에 따른 설정 파일 SHA 차이와 정책 변경을 구분한다.

## 2026-10-28 실제 결과

독립 예약 비교는 19:34:13.702~19:35:43.653 KST에 실행했다.

| 정상 계정의 응답 | 부하 전 25회 | 부하 중 30회 | 회복 35회 |
| --- | --- | --- | --- |
| 부하와 같은 IP | 200 25회 | 200 9회 / 429 21회 | 200 35회 |
| 다른 IP | 200 25회 | 200 30회 | 200 35회 |

부하 600회는401 171회/429 429회였다. 시험780개는 고유 표식 또는 응답 ID로
상태·경로까지 정확히 대조됐고, 같은 IP 및 독립 출처도 검증했다. App/Loki는
사전 확인2건을 포함해 각각782건이며 요청 ID 다중집합이 일치했다.
정상 계정의429 21건은 모두 REJECTED이며 upstream에 전달되지 않았다.

정상 요청을 매초 부하보다 먼저 예약한 앞선 비교에서는 정상30회가 모두200이었다.
이 결과를 보존하고, 독립 스레드·고정 순환 위상으로 예약을 바꾼 비교를 함께 기록했다.
IP 예산은 같은 출처의 사용자를 구분하지 않으며, 정상 요청의 도착 순서도 영향을 준다.
관측된21/30을 서비스 전체의 고정 실패율로 일반화하지 않는다.

준비 중 preflight 소켓의 유휴시간 초과와429 응답 ID 누락으로 중단한 두 실행도
완주 결과와 분리했다. preflight 소켓은 기다리기 전에 닫고, GET UUID 관측은
그 이후 일시 추가했다. 기존 rate/burst·인증·라우팅은 유지했고 종료 후 원래 사이트
SHA `bce9ee9267de817c1dc8104cba6452ad4104b828636f9161d90d395dc35fe077`로 복구했다.
합성 SID와 비공개 pool은 폐기했다. 원본·한국어 그래프·실패·폐기 응답은 로컬
`.local-docs/evidence/456-shared-ip/`와 `456-nginx-shared-ip-impact.md`에 보존한다.
