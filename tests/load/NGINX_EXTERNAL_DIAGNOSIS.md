# V1 외부 접속 실패 단계별 재현

대상은 `https://v1-staging.yeodam-2gether.com` 하나다. Nginx 연결 부족이 없어도 외부 TCP/TLS 실패를 놓칠 수 있어, App 내부 조회와 독립적인 외부 표본 및 ENA 계수 증분을 함께 남긴다. 이전 공격의 정확한 도구·패턴·LTE 오류를 모르므로 동일 공격의 완전 재현으로 해석하지 않는다.

## 역할과 기록

| 실행 위치 | 도구 | 기록 |
| --- | --- | --- |
| Mac, 독립 외부 출처 | `scripts/nginx-external-diagnosis.py --mode probe` | 새 연결로 `/login`과 `/api/actuator/health`를 교대로 조회. DNS·TCP·TLS·첫 바이트·전체 시간·요청 ID |
| 기존 V1 App | 같은 파일의 `--mode ena` | `ens5` ENA allowance 계수를 1 초마다 기록. 처음 누적값과 이후 증분 구분 |
| 기존 V1 App | `nginx-rehearsal.py`, `nginx-tcp-normal-monitor.py` | 1 초 자원·로그, 합성 계정의 지도·목록·상세 조회. 세션은 App 에서만 발급·폐기 |
| 기존 발생기 | `nginx-edge-replay.js`, `nginx-tcp-capacity.py` | 인증 없는 HTTP 반복, TLS 연결 유지·교체. 생성·AI 작업을 새로 등록하지 않음 |
| Mac | `run-nginx-external-trials.py` | AWS 계정과 정확한 대상 확인, 단계 실행·중단·상태 원본 보존 |

probe 는 별도 curl 프로세스를 실행한다. 브라우저 캐시·쿠키·로그인 없이 새 TCP/TLS 연결을 시험하며 실제 화면 렌더링이나 로그인 완료를 증명하지 않는다. `curl -q --noproxy '*'`로 개인 curl 설정·프록시가 실험에 섞이지 않게 한다. 응답 body 와 쿠키는 보존하지 않고 임시 헤더에서 요청 ID 만 추출한다.

시간은 UTC ISO8601 로 저장한다. curl 의 dns/tcp/tls/ttfb/total 은 요청 시작 이후의 누적 시각이며 각각의 구간 소요 시간으로 더하지 않는다. DNS 가 끝났는지 불명확한 timeout 은 `dns-or-tcp-timeout`으로 남긴다. 로컬 curl 미지원(exit4)을 서버 TLS 장애로 분류하지 않는다.

## 실행 준비와 기동

로컬 실행 위치는 Cloud 저장소다. App·발생기·PLG 는 기존 자원을 재사용한다. 실행 도구는 SSM 으로 `/opt/yeodam/ops/nginx-edge438`에 따로 배치한다. 서비스 구성·보안 그룹·Nginx·방화벽을 수정하지 않는다.

App 에는 `nginx-rehearsal.py`, `nginx-rehearsal-archive.py`, `nginx-connection-observer.py`, `nginx-create-resources.py`, `synthetic-session-pool.py`, `nginx-tcp-normal-monitor.py`, `nginx-external-diagnosis.py`가 필요하다. PLG 에는 같은 버전의 archive 도구, 발생기에는 capacity 도구와 edge-replay 시나리오를 둔다. SSM 문서 97KB 제한에 걸리면 코드를 나누거나 압축 전송하고 파일 SHA256 을 대조한다. 압축에 인증 파일을 섞지 않는다.

부하 전에 AWS 계정 `483175530259`·서울 Region 과 실제 인스턴스를 확인한다. App=`i-05d320e3a87200d5a`, 발생기=`i-0c53b65eb35dd2b42`, PLG=`i-05c76ccb2b53f9279`다. V1 사이트·main SHA 가 이전 방어 설정과 같은지 확인하고 새 UUID 로 호스트 수집을 시작한다. 별도 정상 monitor UUID 를 capture 의 `plan.normalRunIds`에 연결해야 함께 내보낼 수 있다.

외부 probe 의 예시다. `edge438_external_id`는 새 UUID, 출력 디렉터리는 새 경로여야 한다. 실행 전에는 `--execute`를 빼서 대상과 시간 제한을 검사할 수 있다.

```bash
edge438_external_id="$(python3 -c 'import uuid; print(uuid.uuid4())')"
python3 tests/load/scripts/nginx-external-diagnosis.py \
  --mode probe --run-id "$edge438_external_id" --seconds 1200 \
  --confirm-target https://v1-staging.yeodam-2gether.com \
  --output "/private/tmp/edge438-$edge438_external_id" --execute
```

App 에서는 같은 CLI 를 `--mode ena`로 실행하며 출력 위치는 해당 capture 의 `ena/` 하위 폴더로 한다. capture 자체와 ena 폴더를 미리 같은 이름으로 만들지 않는다. systemd 의 실행 시간·CPU·메모리 제한을 별도로 둔다. 이번 실행의 상한은 1200 초, host/normal128MiB·20% CPU, ENA64MiB·10% CPU 였다.

부하를 시작할 조건은 host·ENA checkpoint5 초 이내, 내부 정상 조회 10 초 이내, 외부 checkpoint10 초 이내 및 오류·실패 0 이다. 수집이 없거나 오래됐으면 부하를 시작하지 않는다.

## 단계와 중단

`run-nginx-external-trials.py`는 context JSON 에서 captureId·normalId·app·generator·ops·siteSha·mainSha 를 읽는다. UUID 와 정확한 자원·경로를 검증한다. `--execute`가 없으면 계획만 출력한다.

```bash
python3 tests/load/scripts/run-nginx-external-trials.py \
  --context /private/tmp/edge438/context.json \
  --external /private/tmp/edge438/external \
  --output /private/tmp/edge438/trials --execute
```

기본 순서는 HTTP100, HTTP450, TLS 유지 192 개(신규 20 회/s), 교체 120 회/s, HTTP450+교체 120 혼합이다. 각 부하는 30 초, 발생기 unit 은 45 초·512MiB·150% CPU 상한이다. 45 초는 클라이언트 종료·회복 요청을 위한 여유를 포함한다. `--stages`로 정의된 단계만 골라 재개할 수 있지만 기존 출력과 실패 자료를 덮어쓰지 않는다.

HTTP 시나리오는 5 초간 20RPS 이하 준비, 5 초 목표까지 상승, 남은 20 초 목표 유지로 시작 영향을 줄인다. 전체 30 초를 목표 450RPS 상수 부하라고 쓰지 않는다. 최초 100RPS 는 준비 없는 버전으로도 별도 시험했다. 목표를 즉시 450 으로 시작한 첫 시험에서는 27 dropped iterations 가 있었고 그 결과는 실패로 보존했다.

인증·CSRF 가 없는 POST 는 V1 에서 `403 CSRF_TOKEN_INVALID`로 거절됨을 먼저 확인했다. 400·401·403·429 는 이 부하의 예상 음성 응답이며 정상 사용자 성공이 아니다. 201 이나 5xx·전송 실패는 즉시 중단한다. 최초 스크립트가 403 을 예상하지 못해 중단한 결과도 삭제하지 않았다.

외부 probe 의 실패·수집 오류·오래된 checkpoint 가 있으면 진행 중 발생기를 멈추고 다음 단계로 가지 않는다. probe 자체는 3 회 연속 실패에 GUARD 파일을 남기지만 controller 는 첫 실패에서도 중단하므로 더 보수적이다. 외부 정상 요청의 가드는 HTTP200·전체 2 초 이하다. 내부 정상 조회는 기존 1 초 가드를 유지한다.

각 단계 전후에 메모리 512MiB 미만·root80% 이상·최근 5 표본 연속 CPU95% 초과·수집 오류·방어 SHA 변화를 검사한다. ENA 누적 기준이 reset 돼 증분을 계산할 수 없거나 새 allowance 초과가 관측되면 확대하지 않고 자료를 보존한다. 부하 도중 호스트 지표는 전후 검사이며, 외부 probe 가 즉시 중단 신호를 제공한다. 호스트 한도 초과의 순간을 항상 즉시 중단한다고 주장하지 않는다.

## 마감과 확인

발생기 단계가 끝난 뒤 회복 표본을 남기고 외부 출력 폴더·host capture·ENA 폴더·내부 normal 폴더의 `STOP`을 사용해 이번 수집만 마감한다. normal summary 의 `credentialsRevoked=true`와 cleanup 오류 0 을 확인한다. 상시 PLG 와 방어는 유지한다.

`fetch-nginx-rehearsal.py --ops-directory /opt/yeodam/ops/nginx-edge438`로 내보내면 App·Loki 접근 로그·오류 분류·33 개 패널 질의·CloudWatch 와 ENA 원본을 보존한다. App/Loki request_id 다중집합과 파일 checksum 을 대조한다. 외부 probe 와 발생기 결과는 따로 합쳐야 한다.

외부 실패·ENA 증분이 재현되지 않으면 “원인 없음”이 아니라 “이 조건에서 신고를 재현하지 못함”이다. 이전 누적 5037·195·2565 를 이번 실험의 drop 으로 옮겨 쓰지 않는다. LTE 클라이언트와 Mac 이 같은 경로라고 가정하지 않는다.
