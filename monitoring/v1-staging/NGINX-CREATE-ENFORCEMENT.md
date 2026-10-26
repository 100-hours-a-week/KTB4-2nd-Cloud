# V1 생성 요청 차단과 관측 — #433

대상은 `https://v1-staging.yeodam-2gether.com`, App `i-05d320e3a87200d5a`,
기존 발생기와 PLG다. #430의 정상 호출·Dry Run 결과를 근거로 정확한 `POST /api/trips`를
실제 peer별 `1r/s·burst5·nodelay`로 제한한다. 초과 요청은 Nginx429로 끝낸다.
조회5r/s·burst30, client Keepalive15초, 사진 전송600초 timeout은 유지한다.
운영·V2 QA·BE 이미지·DB 인덱스는 변경하지 않는다.

## 먼저 보완한 관측

`nginx-create-resources.py`의 1초 상세 수집에 다음을 추가했다.

- 호스트 `/proc/stat`의 user/nice/system/idle/iowait/irq/softirq/steal 비율.
  guest가 이미 user/nice에 포함되므로 분모에 다시 더하지 않는다. 첫 표본·카운터 역행은 null이다.
- 지정 FE/BE의 Docker ID·init PID와 cgroup v2를 연결한 프로세스 식별.
  FE의 실제 comm은 `next-server (v`여서 `node` 이름만 찾는 방법으로는 누락된다.
- 컨테이너 `cpu.stat`의 누적 usage_usec 차이로 전체 CPU를 계산한다. 100%는 논리 CPU1개이며
  호스트 CPU100%와 분모가 다르다. 컨테이너 총량과 그 안의 프로세스 CPU를 합산하지 않는다.
  표본 사이 종료한 작업은 컨테이너 총량에는 남아도 개별 PID 계산에는 없을 수 있다.
- 기존 DB Threads·row lock·pending I/O 카운터는5초마다 유지한다. Performance Schema는 켜져 있고
  statement digest 집계·metadata lock instrument도 사용 가능함을 읽기 전용으로 확인했다.
  JDBC pool·요청별SQL 시간·모든 lock 대기를 측정한 것은 아니다. 소비자나 로그 설정을 켜지 않았다.

Nginx에는 `upstream_header_time`을 추가하고 원본 반출 목록에도 connect/header/response 시간을 보존한다.
header 시간은 upstream 응답 헤더 수신까지의 누적 시간이며 SQL 실행 시간만을 의미하지 않는다.
[Nginx upstream 변수](https://nginx.org/en/docs/http/ngx_http_upstream_module.html#variables),
[cgroup v2 CPU](https://docs.kernel.org/admin-guide/cgroup-v2.html),
[/proc](https://docs.kernel.org/filesystems/proc.html)를 계산 근거로 사용했다.

## 429도 클라이언트와 로그를 연결하기

첫 실제 차단 시험의 서버 로그는 모두 수집됐지만 Nginx429 응답에는 X-Request-ID가 없었다.
클라이언트236건을 서버와 건별 연결하지 못한 최초 결과를 보존하고, 생성 named location에 다음을 추가했다.

```nginx
proxy_hide_header X-Request-ID;
add_header X-Request-ID $yeodam_staging_request_id always;
```

BE로 넘기는 ID와 같은 값을 반환한다. BE 응답의 기존 헤더는 숨겨 중복을 막고 Nginx429에도 반환한다.
로컬 echo backend가 ID를 반환하는 조건으로 정상 응답에 정확히1개, 생성429에도32자 ID가 있음을 확인했다.
조회429까지 수정한 것은 아니다. 추적 보완 후 초과 단계만 재시험했다.

## 적용과 안전 원복

Cloud의 `monitoring/v1-staging/`에서 아래 파일을 App root 전용
`/opt/yeodam/ops/nginx-create-enforcement/`에 설치한다.

- `install-nginx-create-enforcement.py`, `nginx_create_enforcement.py`, `nginx_create_dry_run.py`
- `nginx_read_enforcement.py`, `nginx_read_dry_run.py`, `nginx_observability.py`

App SSM에서 현재 SHA와 변경 내용을 먼저 확인한다. 실제 시험의 최종 SHA는
`bce9ee9267de817c1dc8104cba6452ad4104b828636f9161d90d395dc35fe077`이다.

```bash
sudo sha256sum /etc/nginx/sites-enabled/yeodam-v1-staging
# 첫 적용에서 observe는 생성 Dry Run을 유지하며 관측 필드만 추가한다.
sudo python3 /opt/yeodam/ops/nginx-create-enforcement/install-nginx-create-enforcement.py observe \
  --expected-site-sha256 '<현재 설정을 검토한 SHA>'
# 수집을 확인한 후 실제 차단으로 전환한다.
sudo python3 /opt/yeodam/ops/nginx-create-enforcement/install-nginx-create-enforcement.py enforce \
  --expected-site-sha256 '<observe 후 SHA>'
```

백업·`nginx -t`·graceful reload를 수행하고 실패하면 원본을 복구한다. 컨테이너·DB를 재시작하지 않는다.
현재 실제 차단을 생성 Dry Run으로 안전하게 돌리는 명령은 다음과 같다.

```bash
sudo python3 /opt/yeodam/ops/nginx-create-enforcement/install-nginx-create-enforcement.py dry-run \
  --expected-site-sha256 bce9ee9267de817c1dc8104cba6452ad4104b828636f9161d90d395dc35fe077
curl -fsS --max-time 5 https://v1-staging.yeodam-2gether.com/api/actuator/health
```

예상 SHA는 `485d8263b238a37bd748e991ab57768a350248093ae91f2641536116f49239bf`, 헬스는UP이다.
이 원복은 실제로 실행한 뒤 최종 차단 SHA로 재적용했다. 다른 작업으로 현재 SHA가 바뀌면 자동 적용을
거절하므로 임의로 예상값만 바꿔 재실행하지 않는다. `rollback --backup <apply 경로>`도 백업의 전후 SHA가
일치할 때만 가능하다. 최신 정상 Dry Run 백업은 `apply-ab99tvh7`이다.

## 실제 시험 결과 — 2026-10-26 KST

| 조건 | 생성201 | Nginx429 | 별도 정상 조회200 |
| --- | ---: | ---: | ---: |
| 1RPS·60초 | 61 | 0 | 31 |
| 같은IP의3계정 각1건 | 3 | 0 | 3 |
| 같은계정3건 | 3 | 0 | 3 |
| 5RPS·60초, 실제 FE 병행 | 65 | 236 | 31 |
| 추적 ID 보완 후5RPS·60초 | 65 | 236 | 31 |

발생기는 k6v2.3.0·고정200VU·HTTP/2·재사용 허용·retry/redirect 없음이다. 기존1초 시험 가드를 유지하며
SLO로 주장하지 않는다. 정상 단계는201만 허용한다. 초과 단계에서만 명시적 옵션으로201/429를 구분하며
429를 성공한 여행으로 계산하지 않는다. 두5RPS 시험과 모든 정상 단계의 Dropped는0이었다.

첫5RPS와 다른 출처에서 실제3탭·재시도용1탭을 함께 사용했다. 생성4건 모두201/PASSED, 사진4건 모두200이었다.
사진1건은 전송 전에 실패시켰다가 같은 여행2075854에 재전송했다. 추가 생성·완료 후refresh 생성은0이었다.
네 여행 모두COMPLETED·사진 행 각1개, S3 원본 각4397665B를 확인했다. 본문 checksum을 대조한 것은 아니다.
처음3개 사진 응답은 약45~47초, 재시도 사진은3.861초였다. Worker 기동·개별 AI 단계가 같은 조건이라는
근거가 없으므로 이를 사진 처리 성능 개선으로 비교하지 않는다.

첫CPU 피크86.034% 표본에서 FE 컨테이너0.002%, BE 컨테이너154.409%(논리 CPU1개 기준)를 확인했다.
Java PID는36.148%였다. 이 표본은 FE만의 누락을 보완하는 데 그치지 않고 컨테이너 전체 사용량을 보여준다.
특정 자식 프로세스·SQL·함수가 유일한 원인이라고 단정하지 않는다.

두 수집 구간의 1초 표본545개와 App/Loki905개 요청 ID를 보존했으며 누락·추가는0이다.
최종 반복 시험의 거절236건은 클라이언트/서버 ID가 모두 일치하고 upstream 연결·DB 반영이0이었다.
첫 시험의 응답 ID 누락236건은 별도 한계로 보존했다. 실제 FE를 포함한 신규 여행201개와
기존 합성 소유 여행을 유지했으며 시험 세션·비공개 임시 파일만 폐기했다.
수집 구간 사이 공백은 그래프에서 연결하거나0으로 채우지 않는다.

## 다음 부하에서도 같은 자료를 남기기

App의 `/opt/yeodam/ops/nginx-create-433/`에는 최신 수집기·상세 sampler·아카이브 도구를 설치했다.
부하 **전에** 새 UUID로 `nginx-rehearsal.py start --detailed-processes`를 실행하고 status의 최신 표본·오류0을
확인한다. CPU/로그 예산 등 기존 제한은 유지한다. 끝나면 stop 후 다음 명령으로 보존한다.

```bash
# Cloud checkout에서 실행. output은 아직 없는 새 폴더여야 한다.
python3 tests/load/scripts/fetch-nginx-rehearsal.py --run-id '<수집 UUID>' \
  --ops-directory /opt/yeodam/ops/nginx-create-433 --output '<원본을 보존할 절대 경로>'
python3 tests/load/scripts/analyze-nginx-create-dry-run.py --evidence '<전체 시험 원본 경로>'
python3 tests/load/scripts/render-nginx-create-dry-run.py --evidence '<같은 경로>'
```

분석 폴더의 capture*/app·plg와 generator/<UUID>·client 구조는 #433 로컬 원본을 참고한다.
차단은 `limit_req_status=REJECTED`, HTTP429, upstream 미연결, DB 미반영을 함께 대조한다.
정상 생성·사진·조회는 별도로 판정한다. 같은 NAT의 정상 생성과 과다 생성이 경쟁하면 정상 사용자도429를
받을 수 있다. 분산 출처·TCP/TLS 고갈까지 해결했다는 주장이 아니며 강사 최종 재부하는 별도다.
