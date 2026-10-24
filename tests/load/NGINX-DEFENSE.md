# Nginx 방어 시험 준비

이번 변경은 **로컬 준비만** 수행한다. Nginx·배포·AWS·DB·사진을 변경하지 않으며 시험을 자동 실행하지 않는다. 과거 장애는 원인 분석의 출발점이고, 방어 효과는 아직 측정하지 않았다.

## 먼저 완료할 일

DB 마이그레이션 담당이 시험과 결과 대조·복원을 마친 뒤 V1 출발 환경의 사용 시간을 조율한다. 별도 Git 브랜치는 실행 서버의 격리를 뜻하지 않는다. 실행 이미지/Digest, DB 연결과 Schema, 실제 Multipart/S3 직접 업로드 경로, Nginx의 실효 설정과 Worker/FD 한도, 시험 대상 DNS를 확인한 후 실행 범위를 정한다. 이 문서의 URL이나 경로는 현재 서버 확인을 대신하지 않는다.

인증된 조회와 10장→11장→150장 업로드가 먼저 정상이어야 한다. 이미 실패하는 BE·AI 경로를 Nginx 방어 결과와 섞지 않는다. 정상 사용자 흐름은 기존 `view-baseline.js`, `upload-baseline.js`를 이용하며 실제 실행은 이후 사용자가 수행한다.

## 준비한 도구와 범위

| 도구 | 목적 | 제한 |
| --- | --- | --- |
| `scenarios/nginx-request-repeat.js` | 인증 없는 지도 GET을 유한 RPS로 반복 | 정확한 V1 스테이징 Origin·명시적 opt-in·조건 필수. Redirect와 자동 Retry 없음 |
| `scripts/nginx-idle-probe.py` | 완전한 HTTP/1.1 요청/응답 뒤 연결이 언제 닫히는지 관찰 | 기본은 계획 출력. 서버가 닫은 연결을 재개설하지 않음. 미완성 Header나 Slowloris 시험 아님 |
| `scripts/summarize-nginx-defense.py` | 이미 내려받은 로컬 로그를 UTC 창으로 집계 | 원본 조회·SSM·서버 수집 없음. 설정 전후 효과를 자동 판정하지 않음 |

요청 반복은 최초 준비 상한100 RPS·300초·최대512 VU다. 유휴 관찰은64개 연결·연결별1~60초·개설 간격0.05~5초·요청 Timeout1~10초다. 이들은 도구의 초기 실행 범위이며 측정한 서비스 한도나 9/29 부하의 재현값이 아니다. 특히64개 유휴 관찰만으로 과거 약1,550개 Host TCP 상태를 재현했다고 쓰지 않는다. 충분한 재현 범위는 실효 설정과 정상 기준선을 확인한 뒤 다시 정한다.

## 지금 로컬에서 할 수 있는 확인

실행 위치는 Cloud 저장소의 루트다. 다음은 서버 요청을 보내지 않는다.

```bash
python3 -m unittest discover -s tests/load/tests -p 'test_nginx_defense.py' -v
node --test tests/load/tests/nginx-defense.test.mjs
node --check tests/load/scenarios/nginx-request-repeat.js
```

Python 시험은 가짜 HTTP transport를 사용한다. 실제 TLS/HTTP 호환성·k6 Runtime·서버에서의 연결 수와 방어 효과는 이 검사로 확인되지 않는다.

연결 관찰의 **계획만** 확인하는 예시도 저장소 루트에서 실행한다.

```bash
python3 tests/load/scripts/nginx-idle-probe.py \
  --target https://v1-staging.yeodam-2gether.com \
  --run-id local-plan --connections 2 --hold-seconds 5 \
  --open-interval 1 --request-timeout 3
```

예상 결과는 `executionEnabled: false`, HTTP/1.1, 재접속 없음과 최대 예상 시간을 담은 JSON이다. 결과 파일도 생성하지 않는다. 이 프로그램이 실제 연결을 열려면 나중에 `--execute`, 동일한 `--confirm-target`, 존재하지 않는 `--output` 파일을 함께 지정해야 한다.

## 나중에 요청 반복을 실행할 때

`config/nginx-defense.example.env`를 참고해 고유 Run ID, RPS·시간·VU·재사용·프로토콜을 명시한다. 예시의 `K6_DEFENSE_EXECUTE=0`은 실행을 거절한다. DB 시험 종료와 대상/시간/상한 확인 후 사용자가 명시적으로1로 바꾸고, `tests/load`에서 기존 `scripts/run-k6.sh`에 `scenarios/nginx-request-repeat.js`를 전달한다. 기존 래퍼는 k6 v2.3.0을 요구하고 요약·실행 로그·가능한 발생기 자원을 Run directory에 남긴다.

- 기본 요청에 인증 Cookie·Authorization을 넣지 않는다. 401과429는 이 재현 시나리오에서만 기대 응답이다. 정상 사용자 시나리오에서 이 코드를 성공으로 처리하지 않는다.
- 상태0·예상 밖 상태·프로토콜 불일치가 나오면 요청 시험을 중단한다. `dropped_iterations`도 실패로 남긴다. 서버 오류가 생긴 뒤 무조건 다음 단계로 부하를 올리지 않는다.
- 재사용 false/true 조건은 실제 HTTP 버전과 함께 기록한다. VU를 TCP 연결 수로 환산하지 않는다. `response.proto`가 사전에 정한 조건과 다르면 cohort를 섞지 않는다.
- 유휴 관찰은 **발생기에서 본** 응답 후 EOF/닫힘 또는 대기 상한이다. Nginx `Active/Waiting`이나 Host TCP와 같은 값이 아니다. 서버가 먼저 닫으면 정상 관찰 결과로 남긴다.
- 각 변경 전후에는 이전 요청·AI 작업과 연결이 정리됐는지 확인한다. Nginx Reload도 비교 변수로 기록한다.

## 이미 보존한 로그를 오프라인 집계

입력 Access는 한 줄당 Nginx JSON 또는 CloudWatch `{timestamp,message}` 이벤트 하나다. CloudWatch의 message 안에는 Nginx JSON이 있어야 한다. JSON 배열·콘솔 표는 먼저 이 JSONL 형식으로 변환해야 한다. 타임존 없는 Access timestamp는 거절한다.

Error는 CloudWatch 이벤트 JSONL 또는 Nginx 원문이다. 원문에는 타임존이 없으므로 **해당 Host의 실제 타임존**을 `--error-utc-offset`에 지정한다. 아래 파일명과 시각은 사용법 예시이며 실제 로그가 존재한다는 뜻이 아니다.

```bash
python3 tests/load/scripts/summarize-nginx-defense.py \
  --access /private/tmp/nginx-access.jsonl \
  --error /private/tmp/nginx-error.log --error-utc-offset +00:00 \
  --start 2026-10-18T00:00:00Z --end 2026-10-18T00:01:00Z \
  --run-id example --output /private/tmp/nginx-summary-example
```

새 디렉터리에 `summary.json`, `routes.csv`, `minutes.csv`가 생성된다. 시작 포함·끝 제외이고 분 집계는 UTC다. JSON은 입력 SHA256과 잘못된 행 수를 포함한다. 오류 행이 있으면 `complete:false`·비정상 종료로 남기므로 성공 결과로 사용하지 않는다. 원본 Cookie·Header·오류 본문을 출력에 복사하지 않는다. p95는 nearest-rank이며 k6 지연 통계와 계산 방식/측정 위치가 다를 수 있다.

추후 로그에 `limit_req_status`·`limit_conn_status`가 추가되면 해당 값을 별도 열로 집계한다. 기존 로그에 없으면 `not_logged`이며, 429만 보고 제한 원인을 추정하지 않는다. 이 준비 작업은 실제 로그 설정을 바꾸지 않는다.

현재 Nginx 로그만으로는 같은 경로의 요청을 정상 사용자와 재현 발생기로 완전히 구별할 수 없다. 로그의 해당 시간창 전체 집계와 발생기별 결과를 분리하고, 필요한 경우 추후 관측 설정에서 실제 출처 구분을 확인한다. 임의 Header를 제한 우회 키로 사용하지 않는다.

## 실제 시험에서 채울 기록

Run ID/시각, Cloud Commit, FE·BE·AI Digest, Nginx 버전과 실효 설정 Hash, Worker·FD·EC2 사양, DB 출발 상태, 업로드 방식, HTTP/재사용 조건, 각 발생기의 실제 출처와 자원, Worker Cold/Warm, Manifest Hash·장수·Byte를 남긴다. 값이 없으면 `미확인`으로 기록한다. Token·사진·전체 nginx 설정을 Git에 넣지 않는다.

| 항목 | 변경 전 | 변경 후 |
| --- | --- | --- |
| 동일 Release·프로토콜·사진 조건 | 미측정 | 미측정 |
| 목표/실측 RPS·dropped·연결 조건 | 미측정 | 미측정 |
| 재현401/429·예상 밖 상태·Timeout | 미측정 | 미측정 |
| 정상 조회 성공률/p95·예상 밖429 | 미측정 | 미측정 |
| 연결 부족 오류·Active/Waiting·회복 | 미측정 | 미측정 |
| 원본150/150·AI 완료·결과 조회 | 미측정 | 미측정 |

실제 AI/원본 검증·서버 연결 관측·중단/복구 책임과 실행 상한은 다음 실행 작업에서 확정한다. 429 증가나 이번 로컬 검증만으로 무중단/방어 성공을 주장하지 않는다.


## #418 실행 후 상태

2026-10-24 사용자 요청으로 k6 직접 실행을 위임받았다. 기존 V1 App에5r/s·burst30의
조회 Dry Run을 적용하고 전용 발생기20RPS×30초와 정상 FE를 검증했다. 실제 차단은
미적용이며 전체 방어가 완료된 것은 아니다. 이전 절의 사용자 직접 실행·로컬 준비만이라는
설명은 최초 준비 이력이다. 현재 수치·제외 범위·원복·측정 한계는
[Dry Run 실행 기록](../../monitoring/v1-staging/NGINX-READ-DRY-RUN.md)을 따른다.

## #420 실제 차단 검증

#418과 같은5r/s·burst30·nodelay를 실제429 모드로 전환한다. 현재 staging renderer도
이를 생성한다. 적용 전 SHA 확인, 별도 백업과 Dry Run 원복, 정상FE·실제 업로드·AI 완료·S3 대조,
비공개 업로더 세션 발급/폐기 절차는 [실제 제한 안내](../../monitoring/v1-staging/NGINX-READ-ENFORCEMENT.md)를 따른다.
#418은 당시 관찰 모드의 비교 기록으로 보존한다. 운영 설정이나 다른 방어까지 적용했다는 뜻은 아니다.
