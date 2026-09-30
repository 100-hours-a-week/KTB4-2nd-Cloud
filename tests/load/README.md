# V1 k6 Baseline 부하 테스트

운영 `https://yeodam-2gether.com`의 Nginx → Backend → S3/MySQL → AI Worker 경로를 현재 설정 그대로 측정합니다. 이 구성은 Nginx 방어 설정, Peak/Spike와 서버 장애까지 진행하는 Breakpoint Test를 포함하지 않습니다.

## 현재 범위

- `upload-baseline.js`: 테스트 계정 인증, 여행 생성, 10장 단위 순차 Upload, 마지막 Batch AI 처리와 완료 여행 조회
- `view-baseline.js`: 이미 완료된 여행의 상세 API를 1 VU로 반복 조회
- `cleanup-trip.js`: Dashboard/Log 검증이 끝난 테스트 여행을 애플리케이션 API로 삭제
- 테스트 데이터 Manifest 검증과 실행 결과 저장

실제 운영 Baseline은 팀 공지, 배포/디버깅 중지, CloudWatch Dashboard 확인 담당자 지정과 즉시 중단 명령 준비 후 실행합니다. Script 작성과 정적 검증만으로 운영 실행을 완료했다고 보지 않습니다.

## 고정 실행 도구

k6 `v2.3.0`을 사용합니다. 실행 머신이 먼저 병목이 되면 App/Worker 결과를 신뢰할 수 없으므로 k6 머신의 CPU/Memory/Network도 함께 확인합니다.

- CPU: 전체 Core가 80%를 지속해서 넘지 않는지 확인
- Memory: 물리 Memory 90% 미만, Swap 미사용 확인
- Network: 송신 대역폭이 발생기 상한에 고정되지 않는지 확인
- 오류: `too many open files`, Dial Timeout과 발생기 OOM 확인

현재 Upload Script는 k6 표준 `open()`으로 Manifest의 파일을 Init Context에서 읽습니다. Baseline은 1 VU로 제한합니다. 150장 데이터셋과 동시 VU를 늘리기 전 실제 Memory 사용량을 측정하고, 필요하면 `k6/experimental/fs` 또는 VU별 데이터 분할을 별도 결정합니다.

## 인증정보와 테스트 데이터

테스트 전용 계정으로 브라우저 로그인을 완료한 뒤 `accessToken` Cookie 값만 실행 환경변수로 전달합니다. Token, Cookie, CSRF 값과 실제 사진은 Git에 저장하지 않습니다.

Manifest는 다음 조건을 만족해야 합니다.

- 사진 1~200장
- 파일당 1B~15MiB
- 전체 3GiB 이하
- `path`는 실행 머신의 절대 경로
- 실제 사용자 식별정보와 불필요한 EXIF/GPS 제거
- AI Pipeline Baseline에서는 GPS 유무, 촬영 시각과 장소 분포가 실제 여행을 대표하도록 별도 익명화

예시는 `fixtures/manifest.example.json`에 있습니다. 실제 Manifest와 사진은 `.gitignore` 대상입니다.

```bash
cd /Users/lee-y.ch/Desktop/yeodam/KTB4-2nd-Cloud/tests/load

./scripts/validate-manifest.sh /absolute/path/to/manifest.json
```

운영 요청 없이 Script 자체만 확인하려면 Multipart 단위 테스트와 Shell 문법 검사를 실행한 뒤 `k6 inspect`를 사용합니다. `inspect`는 Init Context에서 Manifest와 사진을 읽지만 HTTP 요청은 보내지 않습니다.

```bash
node --test tests/multipart.test.mjs
bash -n scripts/run-k6.sh scripts/validate-manifest.sh

K6_BASE_URL='https://example.com' \
K6_ACCESS_TOKEN='static-validation' \
K6_DATA_MANIFEST='/absolute/path/to/manifest.json' \
k6 inspect --include-system-env-vars scenarios/upload-baseline.js
```

`inspect` 출력에서 `upload_baseline` Executor와 `checks`, `http_req_failed`, `yeodam_business_failures`, `yeodam_successful_journeys` Threshold가 보여야 합니다. Manifest 경로나 파일 크기가 다르면 HTTP 요청 전 Init Context에서 종료됩니다.

## 실행 전 기록

실행마다 다음 값을 확인해 환경변수로 전달합니다.

- UTC/KST 시작 예정 시각
- Scenario와 사진 수/전체 Byte
- Cloud/Frontend/Backend/AI Commit
- App/Worker Instance Type
- Worker Cold/Warm 상태
- 테스트 계정과 완료 여행 ID

`config/baseline.example.env`를 복사한 실제 `.env` 파일은 Git에서 제외됩니다. Shell에서 값을 불러올 때는 로그와 Shell History에 Token이 남지 않도록 주의합니다.

## Upload Baseline 실행

먼저 10장, 11장, 30장 순서로 Script와 측정 경로를 확인합니다. 각 단계에서 Dashboard/Log 건수와 결과가 맞을 때만 평균 150장으로 이동합니다.

```bash
cd /Users/lee-y.ch/Desktop/yeodam/KTB4-2nd-Cloud/tests/load

export K6_BASE_URL='https://yeodam-2gether.com'
export K6_ACCESS_TOKEN='test-account-access-token'
export K6_DATA_MANIFEST='/absolute/path/to/manifest.json'
export K6_CLOUD_RELEASE='cloud-commit'
export K6_FRONTEND_RELEASE='frontend-commit'
export K6_BACKEND_RELEASE='backend-commit'
export K6_AI_RELEASE='ai-commit'

./scripts/run-k6.sh scenarios/upload-baseline.js
```

Script는 실제 FE 계약과 같은 순서로 실행합니다.

```text
GET /api/users/me
→ GET /api/auth/csrf
→ POST /api/trips
→ GET /api/auth/csrf
→ POST /api/trips/{tripId}/initial-attachments (최대 10장, 중간 204)
→ POST /api/trips/{tripId}/initial-attachments (마지막 200, AI 완료 대기)
→ GET /api/trips/{tripId}
```

마지막 Batch Timeout은 Nginx의 `/api/trips` 600초보다 길게 15분으로 두었습니다. Nginx/Backend가 먼저 Timeout되면 해당 실패를 그대로 측정합니다.

## 완료 여행 조회 Baseline

초기값은 1 VU, 5분, 요청 사이 2초입니다. `K6_COMPLETED_TRIP_ID`는 테스트 계정이 소유한 완료 여행이어야 합니다.

```bash
export K6_COMPLETED_TRIP_ID='123'
export K6_VIEW_VUS='1'
export K6_VIEW_DURATION='5m'
export K6_VIEW_THINK_TIME_SECONDS='2'

./scripts/run-k6.sh scenarios/view-baseline.js
```

완료 여행 조회의 초기 SLO인 p95 1초를 Threshold로 사용합니다. Upload 중간/마지막 Batch에는 첫 Baseline 전 고정 지연시간 목표를 두지 않습니다.

## 실행 중단 기준

k6 HTTP Threshold만으로 Host/Container 상태를 알 수 없으므로 실행자와 Dashboard 확인 담당자가 함께 판단합니다. 다음 조건에서는 k6에 `Ctrl-C`를 보내 즉시 중단합니다.

- 원본 수와 저장 수 불일치 또는 여행 결과 사진 수 불일치
- Container OOM/비정상 재시작
- App/Worker Status Check 실패
- Nginx/MySQL/Docker Process 중단
- App Root/MySQL Filesystem 80% 이상
- AI 작업 유실 또는 완료 여부를 판단할 수 없는 상태
- 5xx/Timeout이 연속 발생해 정상 요청을 진행할 수 없는 상태
- 발생기 CPU/Memory/Network 포화나 파일 디스크립터 오류

CPU/Memory의 짧은 최대값만으로 즉시 중단하지 않습니다. 높은 사용률이 지속되며 응답시간 악화와 회복 실패가 함께 나타나는지 확인합니다.

## 결과 확인과 정리

`scripts/run-k6.sh`는 `results/<UTC 시각>/`에 다음 파일을 남깁니다.

- `run.log`: k6 출력과 `trip_id`/`request_id`가 포함된 JSON Event
- `summary.json`: k6 Metric/Threshold 요약
- `execution.txt`: 실행 Script/k6 Version/종료 코드

`results/`는 Git에서 제외됩니다. `job_id`는 API 응답에 없으므로, 기록된 마지막 Batch `request_id`와 `trip_id`를 이용해 CloudWatch Logs에서 확인합니다.

Dashboard/Log와 결과 대조가 끝나기 전에는 테스트 여행을 삭제하지 않습니다. 확인이 끝난 뒤 애플리케이션 삭제 API를 사용합니다.

```bash
export K6_TRIP_ID='123'
./scripts/run-k6.sh scenarios/cleanup-trip.js
```

이 삭제는 운영 DB의 논리 삭제이며 S3 Versioning/Lifecycle의 물리 객체 정리와 같지 않습니다. 운영 DB/S3를 직접 수정하는 정리 명령은 이 테스트에 포함하지 않습니다.

## 다음 작업 경계

소량 Dry Run으로 Script와 데이터셋의 실제 Memory/Network 사용량을 확인한 뒤 별도 Issue에서 일시적인 k6 발생기 EC2 사양, 설치/접근/종료 절차를 구성합니다. 이후 운영 Baseline을 최소 3회 반복하고 누락된 Metric/Log를 보완합니다.

Peak/Spike/AI 처리량과 제한적 Stress는 Baseline 결과와 발생기 검증이 끝난 뒤 별도 Scenario로 추가합니다. 이번 구성에서는 Nginx `limit_req`, `limit_conn`을 변경하지 않습니다.
