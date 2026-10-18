# 기존 스테이징 V1 적용 준비

Cloud #317은 기존 V1의 실행 위치와 연결 계약을 확인하고, 설치 파일·V1 통과 프록시·읽기 전용 사전 검사를 준비한다. 추가 EC2를 만들지 않는다. **실제 설치·Nginx 설정 교체/reload·DB/Redis 쓰기·DNS·EC2 전원 조작은 DB 시험 종료 후 실행한다.** `staging-preparation` 프로필은 HOLD와 인증 이행을 거절한다. 설치 준비 검사의 pass는 실제 전환 허가가 아니다.

## 고정한 범위

| 항목 | 준비 설정 |
| --- | --- |
| Account / Region | 483175530259 / ap-northeast-2 |
| V1 호스트 | i-05d320e3a87200d5a, 10.30.0.174 |
| 사용자 Host | v1-staging.yeodam-2gether.com |
| V1 FE / BE | 호스트 loopback3000 / 8080 |
| V1 DB 진입 | loopback16033 → 기존 ProxySQL, yeodam_v1_source |
| 이전 DB 후보 | 10.30.10.28:3306. 최종 Writer UUID는 DB 시험 인계 후 입력 |
| ALB | yeodam-v2-staging-alb-1866428186.ap-northeast-2.elb.amazonaws.com |
| ALB TLS 검증 이름 | staging.yeodam-2gether.com |
| Redis | 기존 BE Redis, TLS6379, run 전용 Prefix |

QA DB `yeodam_validation`/10.30.10.207, 운영 Host/EC2, 다른 ALB·Source와 잘못된 Redis TLS 설정은 거절한다. 실제 ECS 조회 대상은 기존 QA BE Service다. 이 서비스를 자동 재배포하거나 QA DB를 덮어쓰지 않는다. 전환 대상 앱의 연결 설정은 DB 시험과 QA 사용이 끝난 뒤 조율해야 한다.

Nginx 후보는 기존 `yeodam-v1-staging` vhost를 교체할 검토용 파일이다. 원래 vhost와 동시에 enable하지 않는다. 현재 ACME webroot, IPv4/IPv6, TLS1.2/1.3, 본문150MiB, 일반5/60/60초 및 여행600초를 유지한다. 요청은 loopback19091의 gate로 보내고 gate가 초기에는 기존 V1으로 전달한다. HTTP Host와 TLS SNI는 구분하며, gate→ALB는 staging 인증서를 검증하면서 기존 V1 Host를 전달한다.

## 1. 로컬에서 Bundle 생성

실행 위치: 최신 Cloud 작업 저장소 루트. Git 밖의 private 디렉터리를 사용한다. 아래 작업은 로컬 파일만 만든다.

```bash
umask 077
work=/private/tmp/yeodam-staging-cutover-plan
mkdir -m 700 "$work"
python3 - "$work/plan.json" <<'PY'
import json,sys,uuid
from pathlib import Path
p=json.loads(Path('deploy/request-cutover/staging/plan.example.json').read_text())
p['run_id']=str(uuid.uuid4())
p['redis']['prefix']='yeodam:cutover:'+p['run_id']+':'
Path(sys.argv[1]).write_text(json.dumps(p,indent=2)+'\n')
PY
release=$(git rev-parse HEAD)
PYTHONPATH=scripts python3 -m request_cutover.staging render \
  --plan "$work/plan.json" --release "$release" --output "$work/bundle"
bash "$work/bundle/operate.sh" plan
```

같은 출력 경로는 덮어쓰지 않는다. Bundle에는 gate 설정, Nginx 후보, Compose, 운영 wrapper, Python 코드와 의존성, 파일별 SHA256이 들어간다. Hash는 사고성 변경을 확인하는 수단이며 신뢰되지 않은 Bundle을 승인하는 전자서명은 아니다. 코드·계획이 바뀌면 새 Bundle을 만든다. 원본 로컬 fixture의 스키마 보호는 유지하고 별도 준비 프로필만 사용한다.

예상 결과는 `new_servers:0`, `nginx_enabled:false`, `authentication_cutover_enabled:false`다. 계획의 `handoff.db_tests_complete`는 false이고 `mysql.expected_uuid`는 null이다. 시험이 끝나기 전에 편의상 true나 추정 UUID로 채우지 않는다. `source_timezone`도 실제 V1의 날짜 저장 계약을 인계 때 확인해야 한다.

## 2. AWS 설정을 읽고 오프라인 점검

실행 위치: AWS SSO `yeodam-admin`을 사용할 수 있는 로컬 Cloud 저장소. Describe/STS만 호출하고 SSM 실행·Secret 읽기·변경 API는 호출하지 않는다.

```bash
PYTHONPATH=scripts python3 -m request_cutover.staging inventory \
  --plan "$work/plan.json" --output "$work/facts.json"
PYTHONPATH=scripts python3 -m request_cutover.staging_preflight \
  --plan "$work/plan.json" --facts "$work/facts.json" --output "$work/offline-report.json"
```

현재 QA 상태에서는 `v2_uses_migration_database`, `v2_public_contract`, `v2_redis_contract`, `db_test_handoff`가 차단된다. `blocked`의 exit code2는 이 경우 정상적인 보호 결과다. JSON/대상 오류·API 실패는 exit1이다. 5분 지난 inventory, 다른 run/plan, 잘못된 Account·Source·TLS 이름도 통과하지 못한다.

`MYSQL_URL`·issuer·Origin·Callback·Redis endpoint/Prefix 등 공개 설정만 저장하고 JWT Secret·DB/Redis password·Cookie는 출력하지 않는다. 이 검사는 JWT 서명 키 일치나 실제 브라우저 로그인 연속성을 증명하지 않는다. ALB 인증서가 기존 V1 Host도 지원하는지는 `dns_direct_alb_certificate_ready`로 별도 표시한다. upstream TLS가 정상이어도 이 값이 false이면 해당 Host의 DNS를 ALB로 직접 바꾸지 않는다.

## 3. DB 시험 종료 후 호스트 사전 검사

이하 명령은 **지금 실행하는 절차가 아니다.** DB 담당에게 현재 Writer UUID·최종 자료·진행 작업과 시험 종료 근거를 인계받고, QA와 연결 범위를 조율한 뒤 수행한다. 대상은 기존 V1 호스트뿐이다. 별도 서버를 생성하지 않는다.

1. 계획의 handoff 근거·현재 UUID와 검증한 시간대를 입력한다. V2 앱의 DB·공개 Host/Origin/Callback·Redis Prefix를 같은 계획으로 준비한다. Prefix만 바꾸는 것이 QA 격리를 모두 보장하지 않으므로 실제 QA 사용 범위를 먼저 확인한다.
2. 새 Bundle과 5분 이내 inventory를 준비한다. 연결을 확인하려면 Source→현재 Writer와 Redis TLS6379 접근 경계도 승인된 범위로 준비돼 있어야 한다. 이 도구가 SG나 계정을 자동 생성하지 않는다.
3. MySQL은 기존 `yeodam_app`을 사용하되 **별도 READ ONLY transaction에서 고정 SELECT만 실행하고 rollback한다.** ProxySQL 사용자·Hostgroup·DB flag를 변경하지 않는다. Redis는 TLS에서 PING/INFO/SCAN만 실행하고 인증 자료를 쓰지 않는다.
4. 원본 코드가 clean한 정확한 Cloud Commit에서 아래 이미지를 빌드한다. Source와 같은 Linux amd64 플랫폼을 사용한다. Private credentials는 Bundle 밖에 둔다. `.dockerignore`가 코드·의존성만 빌드 입력으로 허용한다.

```bash
# 로컬 빌드 머신: clean Commit과 Bundle release가 같은지 확인한 뒤 실행
image="yeodam-request-cutover:sha-$release"
docker build --platform linux/amd64 -t "$image" -f "$work/bundle/Dockerfile" "$work/bundle"
docker save -o "$work/gate-image.tar" "$image"
```

Bundle·이미지·inventory 전달은 기존 SSM 운영 경로로 한다. SSH를 새로 열거나 private credentials를 Git·공개 S3·채팅에 넣지 않는다. 아래 예시는 기존 Source 호스트에 `/root/yeodam-cutover/bundle`, `facts.json`, 권한0600의 `credentials.json`, `gate-image.tar`가 안전하게 전달된 상태다. JSON key는 `CUTOVER_MYSQL_PASSWORD`, `CUTOVER_REDIS_PASSWORD`이며 값은 해당 환경의 기존 비밀 저장소에서 받는다. `credentials.example.json`의 null 값으로는 연결 검증이 통과하지 않는다.

```bash
# 기존 Source의 SSM root 셸에서만, DB 시험 종료 후
work=/root/yeodam-cutover
docker load -i "$work/gate-image.tar"
release=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["release"])' "$work/bundle/bundle.json")
image="yeodam-request-cutover:sha-$release"
docker run --rm --network host --entrypoint python \
  -v "$work/bundle:/bundle:ro" -v "$work:/private" "$image" \
  -m request_cutover.staging_preflight --host --plan /bundle/plan.json \
  --facts /private/facts.json --credentials /private/credentials.json --output /private/host-report.json
```

IMDSv2로 Source ID/IP를 확인한다. 다른 호스트이거나 DB 시험이 끝나지 않았다면 DB·HTTP·Redis 연결 검사로 진행하지 않는다. 두 DB 경로의 UUID·Writer 상태, V1~V25 스키마, 미완료 사진 작업·미보정 원본 크기, Redis master·비활성 Prefix, ALB TLS·health와 V1 health를 검사한다. 드라이버 오류는 유형만 기록한다. 같은 plan/run의 5분 이내 호스트 보고서가 없으면 install/start를 거절하며 오프라인 pass로 대체할 수 없다.

## 4. 명시적으로 설치하고 V1 통과 확인

```bash
run=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["run_id"])' "$work/bundle/plan.json")
bash "$work/bundle/operate.sh" install --confirm-run "$run" --report "$work/host-report.json"
bash "$work/bundle/operate.sh" start --confirm-run "$run" --report "$work/host-report.json"
bash "$work/bundle/operate.sh" status --confirm-run "$run"
```

install은 Image의 Cloud release label을 대조한 뒤 실제 Image ID로 고정한다. run별 설정과 journal을 만들고 기존 run은 덮어쓰지 않는다. start는 포트 중복을 거절하고 Unix Socket 상태와 metrics 준비를 확인한다. 자동 재시작은 설정하지 않았다. 두 명령 모두 Nginx를 활성화하거나 reload하지 않으며 인증 HOLD·Redis 이행도 실행하지 않는다.

## Nginx 적용과 원상 복구 조건

DB 시험 종료 후 검토한 스테이징 후보 설정만 적용 대상으로 삼는다. 기존 `/etc/nginx/sites-available/yeodam-v1-staging`의 내용과 symlink를 보존하고 후보로 교체한 뒤 `nginx -t`를 실행한다. 실패하면 reload하지 않고 보존한 내용을 복원한다. 성공해도 V1 통과·진행 중 HTTP·화면·로그인을 확인한 뒤 다음 전환 검증으로 넘어간다. 같은 Host의 vhost 두 개를 동시에 활성화하지 않는다.

시험 API의 요청 ID는 GET users/me·POST trips에서 해당 run UUID와 연번 형식만 보존한다. 나머지 요청은 Nginx가 생성한 ID를 쓴다. Nginx가 k6 ID를 덮어써 gate 결과와 대조하지 못하는 문제를 막기 위한 것이다. 로그에는 정규화된 route·status·시간만 기록하고 Query·Cookie·본문을 기록하지 않는다.

준비 프로필의 V1 통과 상태에서 복구할 때는 보존한 V1 Nginx 설정을 먼저 복원하고 `nginx -t`와 graceful reload를 수행한다. 읽힌 설정에19091 참조가 없고 gate의 V1 진행 요청·대기가0이 된 뒤 중지한다.

```bash
bash "$work/bundle/operate.sh" stop --confirm-run "$run"
```

HOLD/V2 상태·진행 요청·Nginx 참조가 남아 있으면 stop을 거절한다. 향후 V2에서 Refresh·Logout이 시작된 뒤에는 옛 SQL 인증으로 자동 복귀하지 않는다. journal·DB·backup 삭제, V1 EC2 중지, DNS 변경은 이 wrapper의 동작에 포함하지 않는다. 실제 인증 이행과 DNS·V1 종료는 합의한 통합 리허설 순서에 따라 검증한다.

## 로컬 검증

```bash
bash tests/ci/check-request-cutover.sh
bash scripts/validate-cloud-pr.sh
```

준비용11개와 기존18개 검사, Nginx1.24→gate→V1의 메서드·URI·본문·CSRF·Cookie·Set-Cookie·요청 ID를 외부 네트워크 없이 검증한다. 실제 Source에서의 root install/start/stop 및 DB·Redis 연결은 DB 시험 종료 후 검증한다.
