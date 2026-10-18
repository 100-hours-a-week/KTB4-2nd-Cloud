# 요청 경로·인증 전환 리허설

이 디렉터리는 **운영 동일 V1 이미지 → 고정 V2 이미지**의 인증 이행과 HTTP 전환 경계를 검증하는 격리 실행 도구다. 기존 QA, DB/CDC 시험, 운영 DNS·서버를 변경하는 Terraform/배포 스크립트가 아니다. `v2-ingress`는 로컬 ALB 대역이다. 이 시험만으로 실제 ALB/DNS 전환이나 EC2 종료를 완료했다고 판정하지 않는다.

## 구조와 권위

```text
HTTPS Nginx → 임시 aiohttp gate → V1 FE / BE
                         └── COMMIT 후 → V2 ingress → V2 FE / BE
                   Unix 제어 socket
                         └── MySQL 인증 5종 → 전용 Redis
```

업무 DB 이전은 별도 리허설이 담당한다. 이 시험은 같은 격리 MySQL의 확대 스키마를 두 앱이 사용하고, **인증 권위만 SQL에서 Redis로 한 번 옮긴다.** 실제 전환에는 업무 DB 이전 완료·현재 Writer 확정이 선행한다.

- Source FE/BE는 `prepare.py`에 고정한 운영 Digest다. V2는 `init` 시점 Cloud Manifest의 Digest를 잠근다. Tag를 다시 해석하지 않는다.
- JWT·Cookie Host는 V1/V2 동일하며 합성 Secret만 사용한다. Redis는 별도 프로세스와 run Prefix를 사용한다.
- Docker 내부 DNS의 운영 Host는 격리 ingress를 가리킨다. 내부 네트워크는 외부 통신을 차단한다. 운영 FE에 baked된 API 주소가 운영으로 나가지 않도록 하는 장치다.
- Kafka/AI/사진 처리는 모의 의존성이다. 실제 GPU·OAuth Provider·사진 10장 처리의 성공을 이 결과에서 주장하지 않는다.
- 오래된 탭은 진행 중 작업이 끝난 뒤 새로고침한다. 구형 multipart API의 영구 호환은 제공하지 않는다.

## 로컬 실행

실행 위치는 이 파일을 포함한 Cloud 작업 저장소 루트다. Docker/Compose, Python3, OpenSSL과 GHCR 읽기 인증이 필요하다. 실행 디렉터리는 Git 밖의 새 경로를 사용한다. `/private/tmp`는 macOS 예시이며 Linux는 `/tmp`의 소유자 전용 경로로 바꾼다.

```bash
runtime=/private/tmp/yeodam-request-cutover-$(date -u +%Y%m%dT%H%M%SZ)
bash scripts/request-cutover.sh init "$runtime"
bash scripts/request-cutover.sh up "$runtime"
bash scripts/request-cutover.sh seed "$runtime" --count 10
bash scripts/request-cutover.sh before "$runtime"
bash scripts/request-cutover.sh check "$runtime"
```

`init`는 새 UUID, 임의 Secret, 시험 전용 인증서(2일), 고정 이미지 목록을 0600 파일로 생성한다. `up`은 MySQL V1..V8 → V2 확대 Migration 순서로 준비한다. `before`는 기존 JWT와 CSRF로 실제 V1 BE를 조회한다. `check`는 진행 중 사진 작업/미보정 원본 크기/현재 Writer/V2 health를 검사한다.

실패 시 `docker compose` 로그와 격리 MySQL의 Flyway 이력을 확인한다. AWS 자격 증명이나 운영 Secret을 주입해 우회하지 않는다. 이미 소비된 시험 인증 자료를 재사용하기보다 새 runtime/run으로 반복한다. `sessions.json`은 Refresh/JWT를 포함하므로 채팅·Git·공개 Artifact에 올리지 않는다.

## 사용자가 실행하는 k6

이 시나리오는 localhost TLS 격리 포트만 허용한다. 아래 명령은 Linux의 k6 v2.3.0에서 시험 CA를 신뢰하는 기준이다. macOS 네이티브의 CA 신뢰 방식은 다르므로 SSL_CERT_FILE만 지정해 같은 결과라고 가정하지 않는다. AWS에서 실행하려면 별도 서버로 SSM 전달한 동일 localhost 포트를 사용하며 QA/운영 도메인으로 제한을 풀지 않는다.

```bash
export SSL_CERT_FILE="$runtime/tls/server.crt"
export K6_CUTOVER_BASE_URL=https://localhost:19443
export K6_CUTOVER_RUN_ID=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["run_id"])' "$runtime/config.json")
export K6_CUTOVER_POOL_FILE="$runtime/sessions.json"
export K6_CUTOVER_RPS=10
export K6_CUTOVER_SUMMARY="$runtime/summary.json"
k6 run --log-format raw tests/load/scenarios/request-auth-cutover.js > "$runtime/k6.jsonl" 2>&1
```

120초 동안 90% 기존 로그인 조회, 10% 실제 `POST /api/trips`의 메타데이터 저장을 수행한다. 사진·AI를 호출하지 않는다. 1/10/30/100 RPS만 허용하고 VU는 초기 RPS×4다. 요청/최종 결과와 반환 Trip ID를 기록하며 자동 재시도는 없다. TLS 검증을 끄지 않는다. `dropped_iterations=0`과 HTTP 확인뿐 아니라 아래 DB 대조까지 통과해야 한다.

다른 터미널에서 같은 runtime 경로를 지정하고 실행한다.

```bash
bash scripts/request-cutover.sh status "$runtime"
bash scripts/request-cutover.sh cutover "$runtime" --confirm-run "$K6_CUTOVER_RUN_ID"
bash scripts/request-cutover.sh after "$runtime"
bash scripts/request-cutover.sh oauth "$runtime"
bash scripts/request-cutover.sh logs "$runtime" > "$runtime/gate.jsonl"
bash scripts/request-cutover.sh metrics "$runtime" > "$runtime/metrics.prom"
```

`K6_CUTOVER_RUN_ID`는 첫 터미널의 값과 같아야 한다. 다른 셸에서는 config.json에서 위와 같이 다시 읽는다. `after`는 V2에서 기존 세션/CSRF, Refresh 회전, Logout 후 기존 JWT 거절을 검사한다. `oauth`는 이행된 State/Ticket/Profile을 실제 V2로 소비하고 재사용 거절을 확인한다. OAuth State는 빈 code로 Provider 호출 전에 소비 여부만 검사한다. Ticket fixture는 수동 시험 여유를 위해 10분으로 생성한다. **실제 DB→Redis 만료는 원래 절대 시각을 그대로 보존하며 수명을 늘리지 않는다.**

`audit.py`는 gate와 k6의 started/final/forwarded/completed를 ID별로 대조하고, 생성 여행의 userId·이름·Trip ID가 DB의 한 행과 일치하는지 검사한다. k6 종료 후 아래 명령을 실행한다. raw 로그 포맷이 아니면 0건으로 실패하므로 원본을 임의 변환하지 않는다.

```bash
# wrapper와 동일한 run별 Compose 프로젝트를 사용한다.
project="yeodam-cutover-${K6_CUTOVER_RUN_ID:0:8}"
docker compose -p "$project" --env-file "$runtime/runtime.env" \
  -f deploy/request-cutover/compose.yml exec -T gate \
  python -m request_cutover.audit --config /private/config.json \
  --k6 /private/k6.jsonl --gate /private/gate.jsonl
```

## HOLD·이행·실패 경계

1. 기존 V1의 모든 유입이 gate를 통과하는 상태에서 시작한다. SSR·관리자·배치 등 gate 우회 Writer가 있으면 진행하지 않는다.
2. 신규 요청은 upstream에 보내기 전 연결을 유지한다. 기존 V1 in-flight=0 뒤 최종 SQL 스냅샷을 읽는다. 사진 처리는 긴 HOLD로 기다리지 않고 미리 완료한다.
3. 로그인 세션, Refresh 역색인, 사용자 ZSET, CSRF, State, Ticket, Profile을 새 Redis Prefix에 넣는다. 사전 복사본을 지우고 최종 집합으로 대조하므로 Logout/소비 삭제도 남지 않는다. DB/Redis 시계 차이가 2초를 넘으면 거절한다.
4. HOLD 기본 예산 5초, 대기 1,000건/본문 길이 예약 64MiB다. 이는 검증된 수용량이 아니며 OS 정지·디스크 정체까지 실시간 상한을 보장하지 않는다. 실제 p95/p99/max를 측정한다.
5. Commit 전 timeout/초과/multipart/불명 body 길이/단절/검증 실패는 V1으로 재개한다. 이미 전달한 요청은 절대 자동 재전송하지 않는다. OAuth GET도 재시도하지 않는다.
6. Commit을 private journal에 fsync한 후 V2만 해제한다. 이후 실패는 Redis 권위를 유지한다. journal 쓰기의 성공 여부가 불확실하면 `BLOCKED`에서 전달을 차단한다. 옛 SQL로 자동 복귀하지 않는다.
7. Controller 파일 잠금이 이행 작업의 중첩을 막는다. timeout으로 V1을 재개한 뒤 복사가 늦게 끝나더라도 다른 Controller가 동시에 Commit하지 못한다.

메모리 대기는 프로세스 장애의 영속 보존이 아니다. 클라이언트 연결이 끊기거나 응답만 유실된 요청은 성공으로 추정하지 않고 request ID/DB 결과로 미확인을 판정한다.

## 기존 사진 크기 보정

`request_cutover.sizes`는 완료 여행의 `files.original_size_bytes IS NULL`만 실제 S3 HeadObject ContentLength로 보정한다. 0/추정 크기를 일괄 입력하지 않는다. 기본은 dry run이며 `--apply --confirm-run UUID`가 필요하다. 이 버전의 명령은 격리 LocalStack 버킷만 허용한다. 운영 S3 권한을 임의 사용하지 않는다. 실제 이전 데이터의 bucket/key/version과 IAM을 확인한 실행 어댑터는 AWS 리허설에서 검증해야 한다.

## 관측

`metrics` 서비스는 제어 Unix Socket의 **GET /metrics만 중계**해 TCP19092에 공개한다. begin/abort/commit은 HTTP로 노출하지 않는다. 기본 게시 IP는 localhost다. 원격 PLG를 붙일 때는 별도 rehearsal SG에서 PLG→19092만 허용하고 exporter를 그 사설 IP에 게시한다.

- `monitoring/request-cutover/prometheus.example.yml`: 1초 scrape 예시. 실제 collector에서 보면 localhost가 어느 서버인지 먼저 확인한다.
- `monitoring/request-cutover/dashboard.json`: phase, in-flight, 대기 수, p95 wait, p99 전체 시간, abort, V1/V2 전달 수, 최종 결과 그래프.
- gate JSON: request ID, 목적지, HTTP status, 대기·전체 시간. 토큰/Query/Cookie/본문을 기록하지 않는다.

현재 파일을 추가하는 것만으로 기존 PLG에 자동 설치되지는 않는다. HTTP 실패·미확인·DB 중복/누락·dropped가 모두 0인지, 전환 동안 새 V1 전달이 없었는지를 대조한다. p95만 보면 짧은 HOLD가 숨을 수 있으므로 p99/max와 원본 요청도 함께 본다.

## V1 종료

```bash
bash scripts/request-cutover.sh stop-v1 "$runtime"
bash scripts/request-cutover.sh stop-bridge "$runtime"
```

두 명령은 V2 commit/현재 데이터 검사/직접 V2 기존 세션 검증을 요구한다. 로컬 V1 FE/BE만 먼저 중지한다. 브리지는 Nginx graceful stop으로 기존 응답을 배출한 뒤 gate를 중지하고, **gate도 꺼진 상태**에서 별도 검증 컨테이너로 V2 직접 조회를 확인한다. 볼륨/백업은 삭제하지 않는다.

실제 AWS에서는 이 명령을 EC2 종료 완료로 취급하지 않는다. 먼저 V1 Worker 작업0·자동 기동 차단, 운영 동일 Host/인증서의 실제 ALB 라우팅, DNS 전환, 잔류 Nginx 유입/연결을 관측해야 한다. 기존 V1 App EC2의 DB/ProxySQL/S3 중계 등 남은 의존이 없음을 확인한 뒤 EC2를 stop한다. DNS TTL만으로 오래된 모든 클라이언트가 사라졌다고 보장하지 않는다. 삭제는 별도 승인 대상이다.

## 자동 검증

```bash
bash tests/ci/check-request-cutover.sh
bash scripts/validate-cloud-pr.sh
```

HTTP 대기·중단·무재시도·URI/헤더/다중 Set-Cookie 보존, uncertain journal 안전 차단과 실제 Redis 타입/절대 만료/삭제 대조를 실행한다. host에 aiohttp가 없다고 핵심 HTTP 검사를 CI에서 생략하지 않는다. 격리 앱 이미지 검증과 실제 AWS ALB·DNS·브라우저·진행 사진의 통합 리허설은 별개의 증거다.
