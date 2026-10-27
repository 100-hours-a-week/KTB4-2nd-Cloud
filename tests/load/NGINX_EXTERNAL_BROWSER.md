# 독립 외부 브라우저와 TCP/TLS 실패 단계 수집

대상은 `https://v1-staging.yeodam-2gether.com`이다. #438의 외부 curl·ENA, #440의 HTTP/TLS 분리 시험에 브라우저 화면과 TCP 메타데이터를 보완한다. 이번 #445는 정상 접속으로 수집 경로를 검증한다. 부하 재시험, 방어 완화, 운영·V2 QA 변경은 포함하지 않는다.

## 왜 세 관측을 함께 남기는가

서버 접근 로그는 HTTP 요청이 도착한 뒤 남는다. DNS·TCP·TLS에서 실패하면 Nginx 접근 로그가 없을 수 있다. 반대로 curl HTTP200만으로 지도·사진·인증 화면의 성공을 증명할 수 없다.

| 관측 | 알 수 있는 것 | 단독으로 알 수 없는 것 |
| --- | --- | --- |
| 독립 외부 브라우저 | 실제 화면, HTTP 상태, Network 오류·Console, 연결 재사용, DNS/connect/SSL timing | 다른 LTE 단말의 경로·오류, 도착하지 않은 패킷의 중간망 위치 |
| 외부 새 연결 curl | DNS/TCP/TLS/HTTP의 마지막 완료 단계, 요청 ID, 실제 협상 HTTP 버전 | 인증 화면·렌더링·OAuth 완료 |
| App TCP 메타데이터 | 확인한 출처의 SYN/SYN-ACK/ACK/RST/FIN과 전송 길이·시각 | TLS 복호화, HTTP 내용, 패킷이 서버에 도착하지 않은 이유 |
| 호스트·ENA·Nginx·PLG | 자원·방화벽·연결 계수와 요청 ID의 서버 처리 | 위 자료가 없는 과거 구간, 클라이언트의 최종 화면 |

Mac은 발생기 EC2와 다른 외부 출처지만 LTE를 대신하지 않는다. 같은 공인 IP를 공유하는 다른 탭·curl 트래픽도 TCP 수집에 포함될 수 있다. 브라우저 CDP connectionId와 커널 flowId를 같은 값으로 취급하지 않는다.

## 시작 전 확인

Cloud 저장소에서 `aws sts get-caller-identity --profile yeodam-admin --region ap-northeast-2`로 계정 `483175530259`를 확인한다. App은 `i-05d320e3a87200d5a`, PLG는 `i-05c76ccb2b53f9279`다. [기존 수집 절차](NGINX_EXTERNAL_DIAGNOSIS.md)대로 새 capture UUID와 별도 normal UUID를 만들고 `plan.normalRunIds`를 연결한다. 호스트·ENA checkpoint와 정상 probe의 최신 시각·오류를 확인한 뒤 외부 시험을 시작한다.

App의 `tcpdump` 설치·버전을 먼저 확인한다. 이번에는 기존 4.99.4를 사용했다. 설치가 없으면 실행 실패를 남기고 시험 전에 준비한다. Nginx나 방화벽 설정을 변경해 관측을 우회하지 않는다. 루트 사용량80% 이상이면 TCP 수집기는 시작을 거절한다.

실행 도구는 SSM으로 App의 `/opt/yeodam/ops/nginx-browser445`에 배치하고 로컬 파일 SHA256과 대조한다. `nginx-tcp-stage-capture.py`와 인증이 필요한 경우 기존 `nginx-fixture-session.py`, `synthetic-session-pool.py`, `private-pool-transfer.py`를 사용한다. archive 도구는 App·PLG 양쪽의 기존 `/opt/yeodam/ops/nginx-edge438`에 같은 버전을 둔다. 토큰·비밀키·사진을 코드 배포 묶음에 넣지 않는다.

브라우저에는 실제 사용자 계정 대신 고정 #416 합성 계정의 새30분 세션 하나를 사용한다. 발급·폐기는 App에서 `nginx-fixture-session.py issue|revoke --run-id <session UUID>`로 수행한다. 서명키는 App 밖으로 옮기지 않는다. 별도로 승인받은 시험 세션만 SSM 암호화 터널과0700 디렉터리/0600 파일로 전달한다. Cookie 값은 출력·Git·HAR에 저장하지 않는다. 기존 사용자 쿠키가 있으면 덮어쓰지 말고 중단한다.

## 브라우저 수집

ego-browser Skill을 읽고 한 TaskSpace의 같은 Page를 사용한다. `scripts/observe-v1-browser.mjs`를 **ego-browser nodejs 안에서** import한다. 일반 Node에서는 Page가 없으므로 실행되지 않는다. 로컬 경로와 space ID는 실제 실행에서 얻은 값으로 고정한다.

```javascript
const capture = await import('file:///absolute/Cloud/tests/load/scripts/observe-v1-browser.mjs');
const task = await taskSpace(EXISTING_SPACE_ID);
const page = task.page('p1');
await capture.begin(page);
// 같은 invocation에서 이동/클릭 → 기대 UI 대기 → 저장한다.
await page.reload();
await page.waitForSelector('실제 snapshot에서 확인한 selector', {state:'visible',timeout:15000});
console.log(await capture.saveRound({
  page, directory:'/private/tmp/새로운-시험/browser', round:'01-login',
  expected:'확인하려는 화면 상태'
}));
```

목표 화면은 익명 로그인, 인증 후 홈 지도, 여행 목록, 대표 여행 상세다. 목록의 로딩 완료·상세의 장소 사진 로딩 완료처럼 화면별 완료 조건을 먼저 확인한다. HTTP200, API의 사진 수150, 현재 viewport의 썸네일 렌더링, 실제150장 업로드 완료는 서로 다른 판정이다. 이 시험은 업로드·AI 작업을 새로 만들지 않는다.

Page 이벤트는 invocation 사이에 이어진다고 가정하지 않는다. `begin`만 하고 다음 invocation에서 저장하면0건이 될 수 있다. 0건은 네트워크 정상의 증거가 아니다. helper는 미종료 App 요청을 최대3초 추가 수집하고 이후에도 pending이면 남긴다. 캐시 우회가 필요하면 해당 Page의 `Network.setCacheDisabled`만 사용하고 끝에 false로 되돌린다. 전역 브라우저 캐시·쿠키는 지우지 않는다.

각 round는 UTC·viewport·요청 메타데이터 JSON, 제한된 HAR, snapshot, 실제 PNG를 저장한다. 요청/응답 body와 전체 헤더·Cookie·쿼리 문자열은 제외하고 응답 `x-request-id`만 허용한다. S3 객체 경로도 제외한다. 따라서 재생 가능한 전체 HAR가 아니다. `unmatchedEvents`, pending, canceled와 누락 timing(-1)을 그대로 보존한다. Console은 문자열을 정제하고 객체 내용은 수집하지 않는다. 화면 PNG 자체에는 보이는 사진·문구가 있으므로 공유 전 별도 검토한다.

수집 경로의 음성 검증은 **브라우저 Page 안에서만** health URL을 `Network.setBlockedURLs`로1회 막아 수행할 수 있다. finally에서 반드시 빈 배열로 원복한다. 이는 inspector/browser-policy 시험이며 서비스 장애나 TLS 실패를 재현한 결과가 아니다. DNS·TCP·TLS 실제 실패를 유발했다고 기록하지 않는다.

## TCP 메타데이터 수집

브라우저의 `/api/users/me` 응답 `x-request-id`를 App 접근 로그에서 찾아 **peer_addr**로 실제 출처를 확인한다. 임의 X-Forwarded-For를 출처로 사용하지 않는다. 출처 매핑은 비공개로 보관한다. IPv6는 별도 확인하며 한 출처의IPv4가IPv6까지 포함하지 않는다.

아래는 **App SSM root shell**의 실행 형식이다. `capture_id`는 이미 시작한 호스트 UUID, `browser_source`는 확인한 단일IP다. 먼저 `--execute` 없이 입력 검사를 하고 실행한다. 같은 UUID의 tcp445 디렉터리는 재사용·덮어쓰지 않는다.

```bash
python3 /opt/yeodam/ops/nginx-browser445/nginx-tcp-stage-capture.py \
  --run-id "$capture_id" --source-ip "$browser_source" --seconds 300 \
  --confirm-target https://v1-staging.yeodam-2gether.com

systemd-run --unit="yeodam-tcp445-$capture_id" \
  --property=RuntimeMaxSec=330 --property=MemoryMax=128M --property=CPUQuota=20% \
  python3 /opt/yeodam/ops/nginx-browser445/nginx-tcp-stage-capture.py \
  --run-id "$capture_id" --source-ip "$browser_source" --seconds 300 \
  --confirm-target https://v1-staging.yeodam-2gether.com --execute
```

대상은 확인한 global IP 1~2개 또는 기존 발생기의 사설 IP로 제한한다. 전체 트래픽이나 CIDR은 허용하지 않는다. ens5·TCP443·비 promiscuous·snaplen96 조건에서 tcpdump의 **텍스트 요약만** 읽는다. IP와 flow는 실행별 비밀 난수로 HMAC 처리하고 패킷 body·pcap·TLS 복호화 데이터를 저장하지 않는다. 최대1800초·10만 패킷·32MiB이며 이번 실측은300초다. IPv6 파서는 단위 검증했지만 실제 IPv6 접속을 측정한 것은 아니다.

출력은 `/var/lib/yeodam/nginx-rehearsal/<capture UUID>/tcp445/`다. checkpoint를 약1초마다 기록하고 `STOP` 파일이나 duration 도달로 종료한다. summary의 packets·parseErrors·kernelDropped·discardedAfterBudget·exitCode·complete를 확인한다. kernelDropped를 얻지 못하면 null이며0으로 처리하지 않는다. budget 초과·파싱 오류는 불완전 수집이다. tcpdump 실행 실패를 서비스의 통신 장애와 구분한다.

TCP 플래그만으로 TLS 실패를 단정하지 않는다. SYN만 남은 flow는 관측 구간 밖에서 이어졌거나 재송신·동일 port 재사용일 수 있다. curl 단계 시간, 브라우저 오류와 UTC·출처·Nginx 요청 ID를 함께 확인한다. 재사용 연결은 화면 조작 중 SYN이 없어도 정상이다.

## 외부 probe와 실패 판독

기존 `nginx-external-diagnosis.py --mode probe`에 `--http-version 2`를 추가한다. 각 curl은 새 연결로 `/login`과 health를2초 간격으로 교대 조회한다. HTTP/2 요구 후1.1로 fallback한 응답은 정상으로 인정하지 않는다. 기존 기본값은1.1이다. 정상 판정2초와 curl 종료 상한5초를 구분하고 exitCode·total·status를 같이 읽는다.

| 기록 | 판정 |
| --- | --- |
| curl DNS 실패, browser ERR_NAME_NOT_RESOLVED | DNS |
| curl connect 실패, browser CONNECTION_REFUSED | TCP 접속 단계 |
| curl TCP 완료·TLS 미완료 timeout, 인증서/SSL 오류 | TLS 단계 |
| curl TLS 완료 후 timeout, HTTP4xx/5xx | HTTP 또는 이후 전송. 429는 방어 응답이며 화면 성공은 아님 |
| browser inspector/CORS blocked | browser-policy. 서버 TCP/TLS 장애로 합산하지 않음 |
| browser canceled | client-canceled. 화면 이동 취소와 서버 실패 구분 |
| browser timeout/reset, 단계 timing 없음 | transport-stage-unknown. TLS로 단정하지 않음 |
| 브라우저만 실패, HTTP/API200 | Console·화면·제삼자 리소스 확인. 200을 기능 성공으로 간주하지 않음 |

curl timing은 시작부터의 누적값이다. CDP connect에는 SSL 구간이 포함되므로 connect+ssl을 합산하지 않는다. 연결 재사용·캐시로 DNS/SSL이-1이면 미측정이며0ms가 아니다. `failureStage`는 관측 분류를 돕고 원인을 확정하지 않는다.

보존 JSON은 네트워크 없이 다시 집계한다. Cloud 저장소에서 아래를 실행하면 round별 완료·pending·browser-policy·실패·서버 요청 ID가 나온다. 화면만 저장한0건 round는 네트워크 성공에 합산하지 않는다.

```bash
node tests/load/scripts/analyze-nginx-browser.mjs /absolute/evidence/browser > /absolute/evidence/browser-analysis.json
```

## 시험 표시·마감·다음 재부하

부하를 재의뢰하기 전에 host/ENA/normal/TCP/외부 probe의 시작·최신 checkpoint를 확인한다. 독립 브라우저와 발생기가 다른 출처인지 확인하고 부하 전·중·후 같은 화면 조작을 각각 round로 저장한다. 부하 시작·종료를 UTC로 남긴다. 다른 invocation에서 오래 기다린 뒤 브라우저 이벤트를 나중에 회수할 수 있다고 가정하지 않는다.

실패 시 화면·시각·URL·Console/Network·curl의 마지막 완료 단계부터 보존하고 TCP/ENA/접근 로그와 대조한다. LTE 실기기의 실패 시각·URL·오류 화면은 별도로 필요하다. Mac 성공으로 LTE 실패 신고를 부정하지 않는다.

normal·ENA·host·TCP를STOP으로 마감하고 전체 summary와 합성 세션 폐기를 확인한다. 브라우저에서는 자신이 주입한 값과 완전히 일치하는 accessToken/CSRF_CONTEXT만 삭제하고 다른 쿠키를 보전한다. 로컬 인증 파일을 삭제하고 TaskSpace를finish한다. 상시 PLG는 유지한다.

마감 후 `fetch-nginx-rehearsal.py --ops-directory /opt/yeodam/ops/nginx-edge438`로 내보낸다. tcp445는 허용한 plan/checkpoint/packets/summary만 포함하고 tcpdump stderr·출처IP·private key는 제외한다. App/Loki 요청 ID 다중집합과 브라우저 응답 요청 ID를 대조하고 파일 checksum·실행 소스를 보존한다. 수집 불능은 null/누락으로 남긴다. IPv6·LTE·OAuth·부하 중 화면 동작에서 미실시한 범위는 검증 완료로 기록하지 않는다.
