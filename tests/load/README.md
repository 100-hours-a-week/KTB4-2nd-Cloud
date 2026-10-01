# V1 k6 Baseline 부하 테스트

운영 `https://yeodam-2gether.com`의 Nginx → Backend → S3/MySQL → AI Worker 경로를 현재 설정 그대로 측정합니다. 이 구성은 Nginx 방어 설정, Peak/Spike와 서버 장애까지 진행하는 Breakpoint Test를 포함하지 않습니다.

## 현재 범위

- `upload-baseline.js`: 테스트 계정 인증, 여행 생성, 10장 단위 순차 Upload, 마지막 Batch AI 처리와 완료 여행 조회
- `view-baseline.js`: 이미 완료된 여행의 상세 API를 1 VU로 반복 조회
- `cleanup-trip.js`: Dashboard/Log 검증이 끝난 테스트 여행을 애플리케이션 API로 삭제
- 테스트 데이터 Manifest 검증과 실행 결과 저장

Upload 원본 수는 마지막 처리 응답의 분류·미분류 사진 수 합계로 검증합니다. 여행 상세의 `attachmentCount`는 화면에 노출되는 사진 수이므로 업로드 원본 수와 직접 비교하지 않고, 생성한 `tripId`가 정상 조회되는지만 확인합니다.

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

사진 Directory에서 10장, 11장, 30장, 150장 Manifest를 한 번에 만들 수 있습니다. 지원 형식은 JPG/JPEG, HEIC/HEIF와 PNG입니다. 최상위 Directory별 사진 비율을 유지하면서 파일의 상대 경로를 SHA-256으로 정렬하므로, 디카와 휴대폰처럼 기기별 Directory를 나눈 데이터에서 한쪽 사진만 먼저 선택되지 않습니다. 같은 Directory와 Version을 사용하면 매번 같은 사진이 선택되고, 적은 장수의 Manifest는 더 큰 Manifest의 부분집합이 됩니다.

```bash
cd /Users/lee-y.ch/Desktop/yeodam/KTB4-2nd-Cloud/tests/load

./scripts/prepare-baseline-manifests.sh \
  '/Users/lee-y.ch/Downloads/eval' \
  "$(pwd)/fixtures" \
  '2026-09-30-eval-v1'
```

생성된 `baseline-10.json`, `baseline-11.json`, `baseline-30.json`, `baseline-150.json`은 Git에 포함되지 않습니다. 발생기 EC2에서는 사진 압축을 푼 절대 경로를 첫 번째 인자로 사용해 Manifest를 다시 만듭니다. Local 경로가 들어간 Manifest를 EC2에 그대로 복사하지 않습니다.

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

## 부하 발생기 준비

Baseline은 App/Worker와 분리한 임시 `c6i.large` EC2에서 실행합니다. 발생기는 외부 Inbound를 열지 않고 SSM으로만 접속하며, 테스트가 끝나면 Terraform에서 제거합니다. 다음 명령은 Cloud 저장소 Root에서 실행합니다.

```bash
cd /Users/lee-y.ch/Desktop/yeodam/KTB4-2nd-Cloud
export AWS_PROFILE=yeodam-admin

aws sts get-caller-identity
terraform -chdir=terraform plan \
  -var='enable_load_generator=true' \
  -out=/tmp/yeodam-v1-load-generator.tfplan
terraform -chdir=terraform apply /tmp/yeodam-v1-load-generator.tfplan
```

Apply 전 Account가 여담 운영 계정인지, Region이 `ap-northeast-2`인지 확인합니다. 최초 Plan은 발생기 EC2, 전용 IAM/SSM과 Security Group `7 added`, Fixture/Result Lifecycle `1 changed`, `0 destroyed`만 허용합니다. App/Worker 교체나 삭제가 보이면 Apply하지 않습니다.

사진과 현재 Load Test Script를 Private S3에 올립니다. 사진은 `load-test-fixtures/`에서 1일 뒤 정리되고, 결과는 `load-test-results/`에서 30일 뒤 정리됩니다.

```bash
fixture_prefix="$(terraform -chdir=terraform output -raw load_generator_fixture_s3_prefix)"
run_id="$(date -u '+%Y%m%dT%H%M%SZ')"

./tests/load/scripts/upload-generator-assets.sh \
  '/Users/lee-y.ch/Downloads/eval.zip' \
  "${fixture_prefix}" \
  "${run_id}"
```

Instance가 SSM Online인지 확인한 뒤 Session을 시작합니다.

```bash
instance_id="$(terraform -chdir=terraform output -raw load_generator_instance_id)"

aws ssm describe-instance-information \
  --filters "Key=InstanceIds,Values=${instance_id}"
aws ssm start-session --target "${instance_id}"
```

SSM Session 안에서는 먼저 Root Shell에서 Setup Script와 Checksum을 받은 뒤 Workspace를 구성합니다. `RUN_ID`는 앞에서 Asset을 올릴 때 사용한 값과 같아야 합니다.

```bash
sudo -i

run_id='RUN_ID'
fixture_prefix='s3://BUCKET/load-test-fixtures'
run_prefix="${fixture_prefix}/${run_id}"

aws s3 cp "${run_prefix}/setup-generator-workspace.sh" /tmp/setup-generator-workspace.sh
aws s3 cp "${run_prefix}/setup-generator-workspace.sh.sha256" /tmp/setup-generator-workspace.sh.sha256
cd /tmp
sha256sum --check setup-generator-workspace.sh.sha256
chmod 0700 setup-generator-workspace.sh
./setup-generator-workspace.sh "${run_prefix}" '2026-09-30-eval-v1'

cat /opt/yeodam-load/bootstrap.txt
k6 version
exit
```

실행은 `ubuntu` 사용자로 전환합니다. Access Token은 명령 인자, S3, `.env`와 Shell History에 남기지 않고 Session에서 숨김 입력으로만 설정합니다.

```bash
sudo -iu ubuntu
cd /opt/yeodam-load/source/tests/load

export K6_BASE_URL='https://yeodam-2gether.com'
read -r -s -p 'K6_ACCESS_TOKEN: ' K6_ACCESS_TOKEN
echo
export K6_ACCESS_TOKEN
export K6_DATA_MANIFEST='/opt/yeodam-load/manifests/baseline-10.json'
export K6_RESULTS_DIRECTORY='/opt/yeodam-load/results'
export K6_CLOUD_RELEASE='cloud-commit'
export K6_FRONTEND_RELEASE='frontend-commit'
export K6_BACKEND_RELEASE='backend-commit'
export K6_AI_RELEASE='ai-commit'

./scripts/run-k6.sh scenarios/upload-baseline.js
```

10장 결과를 Dashboard/Log와 대조하기 전에는 11장이나 30장으로 넘어가지 않습니다. 실행 결과를 확인한 뒤 발생기에서 Private S3 결과 Prefix로 올립니다.

```bash
result_prefix='s3://BUCKET/load-test-results/RUN_ID'
aws s3 cp /opt/yeodam-load/results "${result_prefix}" \
  --recursive \
  --sse AES256 \
  --only-show-errors
```

결과를 Local에 내려받고 Fixture를 정리한 다음 발생기 제거 Plan을 확인합니다.

```bash
cd /Users/lee-y.ch/Desktop/yeodam/KTB4-2nd-Cloud
export AWS_PROFILE=yeodam-admin

run_id='RUN_ID'
fixture_prefix="$(terraform -chdir=terraform output -raw load_generator_fixture_s3_prefix)"
result_prefix="$(terraform -chdir=terraform output -raw load_test_result_s3_prefix)/${run_id}"

aws s3 sync "${result_prefix}" "tests/load/results/${run_id}"
aws s3 rm "${fixture_prefix}/${run_id}" --recursive

terraform -chdir=terraform plan \
  -var='enable_load_generator=false' \
  -out=/tmp/yeodam-v1-load-generator-destroy.tfplan
terraform -chdir=terraform apply /tmp/yeodam-v1-load-generator-destroy.tfplan
```

제거 Plan에는 임시 발생기 관련 리소스만 삭제되어야 합니다. App, Worker, MySQL EBS, Application S3는 삭제 대상이면 안 됩니다.

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

초기값은 1 VU, 5분, 요청 사이 2초입니다. `K6_COMPLETED_TRIP_ID`는 테스트 계정이 소유한 완료 여행이어야 합니다. k6의 기본 동작은 VU 반복마다 Cookie Jar를 비우므로, 이 Scenario는 `noCookiesReset`을 켜고 처음 확인한 Access Token Cookie를 같은 VU에서 유지합니다. 실행 중 401이 발생하면 전체 테스트를 중단하며, 다른 요청 오류가 발생해도 요청 간 대기시간을 유지합니다.

```bash
export K6_COMPLETED_TRIP_ID='123'
export K6_VIEW_VUS='1'
export K6_VIEW_DURATION='5m'
export K6_VIEW_THINK_TIME_SECONDS='2'

./scripts/run-k6.sh scenarios/view-baseline.js
```

완료 여행 조회의 초기 SLO인 p95 1초를 Threshold로 사용합니다. Upload 중간/마지막 Batch에는 첫 Baseline 전 고정 지연시간 목표를 두지 않습니다.

## V1 세션 비율 혼합 한계 시험

V1 설계의 피크 1시간은 여행 생성 4세션·일반 조회 11세션이다. 이는 API 요청 비율이나 고정 VU 비율이 아니다. `mixed-limit.js`는 두 세션의 **시작률**을 4:11로 유지하며 `K6_LOAD_MULTIPLIER`로 함께 늘린다. 기본 1시간에서 배수 1/2/3은 각각 생성 4/8/12건과 조회 11/22/33세션을 시작한다. 응답이 느려져도 예정된 유입은 유지하며, VU 부족으로 시작하지 못한 `dropped_iterations`는 실패로 기록한다.

계정마다 독립된 k6 Scenario와 VU 1개를 배정한다. Scenario 이름의 계정 번호로 Token을 선택하고, 생성·조회 전체 시작률은 계정별로 나눠 합산한다. 1배수에서 생성 계정 2명은 각각 시간당 2세션, 조회 계정 1명은 시간당 11세션을 시작한다. 토큰을 넣기 전 `scenarios/account-assignment-check.js`로 1·2·3배수의 계정 Slot을 HTTP 요청 없이 검증한다.

- 생성 세션: 인증 확인 → 여행 생성 → 150장 10장 단위 업로드 → 마지막 Batch가 AI 완료를 기다리는 동안 2초 간격 처리 상태 조회 → 결과 여행 상세 조회
- 일반 조회 세션: 인증 확인 → 여행 목록 → 지도 → 완료 여행 상세 → 장소 폴더 목록. 요청 사이 기본 2초를 두며 `K6_GENERAL_VIEW_THINK_TIME_SECONDS`로 조정할 수 있다. 설계의 5개 기본 요청을 현재 API로 재현하지만, 로그인 화면/OAuth 자체와 사진·일기 조회까지 포함한 완전한 FE 사용자 여정은 아니다.

운영에서는 서로 다른 카카오 계정으로 정상 가입한 **생성 계정**을 준비한다. 한 생성 VU는 한 계정만 사용하고, 동시 세션이 계정을 공유하지 않는다. 처리시간 변동에 대비해 생성 계정은 부하 배수보다 최소 1개 더 준비한다. 조회 계정에는 해당 계정 소유의 완료 여행 ID를 1:1로 연결한다. Access Token 유효기간은 30분이므로 이 시험은 계정별 Refresh Cookie로 매 세션 시작 시 Access Token을 갱신한다. 생성·조회 계정 사이에도 Refresh Cookie를 공유하지 않는다. Refresh Token은 장기 인증 정보이므로 Shell에서 숨김 입력하고 파일·명령 인자·결과에 저장하지 않는다. 아래는 생성 계정 2개와 조회 계정 1개의 예시다. 배수나 실행시간을 올리기 전에 계정 수와 발생기 메모리 여유를 다시 확인한다. k6가 VU마다 사진 데이터를 적재하므로 조회 VU도 발생기 메모리를 사용한다. 기존 발생기 `c6i.large`에서는 150장 단일 실행의 최대 RSS가 약 1.44GiB였으므로, **이 혼합 시험 전에는 발생기 증설 또는 분리가 필요하다.**

Refresh Token과 조회 계정 소유의 완료 여행 ID를 준비하고 운영 테스트 시간을 확정한 뒤, 실행 직전에만 발생기를 `m6i.xlarge`로 변경한다. 아래 Terraform 명령은 Cloud 저장소 Root에서 실행한다. `plan`에서 기존 발생기 **1 changed, 0 added, 0 destroyed**이며 App/Worker/MySQL 변경이 없는지 확인한 다음에만 `apply`한다. 기본값은 비용을 위해 `c6i.large`로 유지한다. 테스트가 끝나면 같은 절차로 `c6i.large`로 되돌린다. EC2 타입 변경 중 발생기는 잠시 중지되므로 기존 SSM Session은 끊기며, 재접속 후 `free -h`와 `k6 version`을 확인한다.

증설 전, 본 시험에 사용할 세 세션과 **별개로 새로 로그인한 세션**의 Refresh Token 하나로 `scenarios/auth-smoke.js`를 `c6i.large` 발생기에서 1회 실행한다. 인증 갱신과 `/api/users/me`가 모두 200이어야 통과한다. Smoke가 Refresh Token을 회전시키므로 여기에 쓴 Token은 본 시험에 재사용하지 않는다. 실패하거나 계정 Slot 검사에 실패하면 증설하지 않는다.

```bash
cd /opt/yeodam-load/source/tests/load
export K6_BASE_URL='https://yeodam-2gether.com'
read -r -s -p 'smoke 전용 refresh token: ' K6_AUTH_SMOKE_REFRESH_TOKEN; echo
export K6_AUTH_SMOKE_REFRESH_TOKEN
./scripts/run-k6.sh scenarios/auth-smoke.js
unset K6_AUTH_SMOKE_REFRESH_TOKEN
```

```bash
cd /Users/lee-y.ch/Desktop/yeodam/KTB4-2nd-Cloud
export AWS_PROFILE=yeodam-admin
aws sts get-caller-identity
terraform -chdir=terraform plan -var='enable_load_generator=true' -var='load_generator_instance_type=m6i.xlarge' -out=/tmp/yeodam-load-m6i-xlarge.tfplan
terraform -chdir=terraform apply /tmp/yeodam-load-m6i-xlarge.tfplan
```

시험과 결과 백업이 끝나면 `load_generator_instance_type=c6i.large`로 다시 Plan을 확인해 Apply한다. 두 Plan 파일에는 Terraform State의 민감한 값이 포함될 수 있으므로 Git에 넣지 않고 사용 후 삭제한다. 토큰을 아직 받지 못했거나 조회 계정의 완료 여행을 확인하지 못했다면 증설하지 않는다.

```bash
cd /opt/yeodam-load/source/tests/load
export K6_BASE_URL='https://yeodam-2gether.com'
export K6_DATA_MANIFEST='/opt/yeodam-load/manifests/baseline-150.json'
export K6_RESULTS_DIRECTORY='/opt/yeodam-load/results'
read -r -s -p 'creation refresh token 1: ' creation_token_1; echo
read -r -s -p 'creation refresh token 2: ' creation_token_2; echo
read -r -s -p 'view refresh token: ' view_token; echo
export K6_CREATION_REFRESH_TOKENS="${creation_token_1},${creation_token_2}"
export K6_VIEW_REFRESH_TOKENS="${view_token}"
export K6_VIEW_TRIP_IDS='완료된_조회_계정_소유_여행_ID'
export K6_LOAD_MULTIPLIER='1'
export K6_LIMIT_DURATION='1h'
./scripts/run-k6.sh scenarios/mixed-limit.js
```

`K6_CREATION_REFRESH_TOKENS`는 생성 VU당 서로 다른 사용자 Refresh Token을 쉼표로 구분한다. `K6_VIEW_REFRESH_TOKENS`와 `K6_VIEW_TRIP_IDS`는 같은 순서·개수여야 한다. 시작 전에 조회 ID를 실제 정수로 바꾸고, 배포 Release와 계정별 소유권을 확인한다. 브라우저의 `/api/auth` 경로에 발급된 `refreshToken` Cookie 값을 사용한다. 401이 발생하면 시험을 중단한다. 실행은 1시간 유입 후 시작된 생성 세션을 최대 35분 더 기다릴 수 있다. 결과의 `run.log`에는 세션별 `trip_id`·`request_id`, `summary.json`에는 시나리오별 실제 시작/완료 수와 `dropped_iterations`가 남는다. Token은 결과 이벤트에 기록하지 않는다.

운영 테스트 시간을 공지하고 배포를 멈춘 뒤 배수 1부터 시작한다. 각 단계의 k6 결과와 CloudWatch에서 생성 성공/실패·원본 수, 조회 성공률과 p95, 처리 상태 Polling, AI 대기/완료, App/Worker/발생기 자원 및 회복을 확인한다. 이전 단계의 작업이 남아 있거나 중단 기준에 닿으면 다음 배수로 올리지 않는다. `dropped_iterations`가 발생하면 먼저 계정/VU 수와 발생기 자원을 점검한다. 이것만으로 App 한계라고 판정하지 않는다. 사진 재사용, 소수 계정·완료 여행 반복 조회, 외부 API 변동은 결과의 대표성 한계로 함께 기록한다. 이 시나리오의 최초 재현 가능한 서비스 미달 지점은 **이 세션 구성에서의 한계**이며 모든 API의 절대 최대 RPS가 아니다.

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
- `execution.txt`: 실행 Script/k6 Version/종료 코드와 실행 전후 Network Byte
- `runner-time.txt`: GNU time 기준 CPU 사용률과 최대 RSS. GNU time이 없으면 미생성 사유 파일 저장

`results/`는 Git에서 제외됩니다. `job_id`는 API 응답에 없으므로, 기록된 마지막 Batch `request_id`와 `trip_id`를 이용해 CloudWatch Logs에서 확인합니다.

Dashboard/Log와 결과 대조가 끝나기 전에는 테스트 여행을 삭제하지 않습니다. 확인이 끝난 뒤 애플리케이션 삭제 API를 사용합니다.

```bash
export K6_TRIP_ID='123'
./scripts/run-k6.sh scenarios/cleanup-trip.js
```

이 삭제는 운영 DB의 논리 삭제이며 S3 Versioning/Lifecycle의 물리 객체 정리와 같지 않습니다. 운영 DB/S3를 직접 수정하는 정리 명령은 이 테스트에 포함하지 않습니다.

이번 변경은 혼합 한계 시험의 구성까지다. 운영 실행과 결과 판정은 계정·발생기·시험 시간 준비 후 별도로 진행한다. Nginx `limit_req`, `limit_conn`은 변경하지 않는다.
