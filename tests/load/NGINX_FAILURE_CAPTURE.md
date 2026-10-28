# 실제 외부 접속 실패 한 건을 남기는 절차

대상은 `https://v1-staging.yeodam-2gether.com`이다. #438·#445의 수집기를 그대로 사용하고, #451에서 발견한 두 공백을 보완한다. 원본 HAR도 지도 화면부터 시작해 인증 이동 기록이 없었고, TCP 수집기는 해당 실행에서 시작하지 않았다. Mac probe의 TLS 지연을 아이폰 LTE나 과거 강사 접속 실패의 원인으로 옮겨 쓰지 않는다.

이번 변경은 로컬 계획·준비 상태 검사·오프라인 대조와 HAR 정제다. 서버 기동·배포·부하·단말 조작을 자동 실행하지 않는다. 실제 실패가 없어도 도구의 음성 표본을 검증할 수 있지만, 그것을 LTE 장애 재현이라고 부르지 않는다. #451의 부하 동시 검증은 별도 미완료 작업이다.

## 하나의 관측에 묶을 자료

| 위치 | 기존 도구 | 이번에 확인할 연결 |
| --- | --- | --- |
| App | `nginx-rehearsal.py` + detailed processes | host·Nginx·FD·TCP 계수·방어 카운터·MySQL 상태 |
| App | `nginx-external-diagnosis.py --mode ena` | 처음 누적값과 해당 실행의 초과 증분 |
| App | `nginx-tcp-normal-monitor.py` | 독립 정상 조회와 자신이 만든 합성 세션 폐기 |
| Mac | `nginx-external-diagnosis.py --mode probe` | 새 연결 DNS·TCP·TLS·HTTP, 요청 ID. 실제 LTE와 별도 출처 |
| App | `nginx-tcp-stage-capture.py` | 확인한 단말 출처 1~2개의 TCP443 메타데이터 |
| 실제 실패 단말 | Network 기록 보존·원본 HAR·오류 화면 | 첫 접속부터 OAuth 이동·복귀까지. 개인 로그인은 사용자 조작 |

HTTP 접근 로그가 없으면 TCP 수집이 필요하고, TCP 수집도 없거나 필터가 틀렸으면 서버 미도착을 주장할 수 없다. TLS payload·pcap·전체 트래픽 수집은 추가하지 않는다. 단말 출처를 못 확인한 첫 접속 실패는 그대로 관측 공백이다. 임의 IP나 전체 CIDR 필터로 대체하지 않는다.

## 새 계획 만들기 — Mac의 Cloud 저장소

아래 도구는 파일만 만들며 AWS를 호출하지 않는다. 기존 디렉터리를 덮어쓰지 않고, 이전 수집 ID를 재사용하지 않는다. 디렉터리 0700, JSON 출력 0600이다.

```bash
python3 tests/load/scripts/nginx-failure-evidence.py prepare \
  --directory /private/tmp/nginx-failure-452-session --seconds 300
```

결과 `plannedOnly=true`, `startedCollectors=0`은 준비 완료가 아니다. `manifest.json`의 captureId·normalId·externalId를 이번 실행에만 사용한다. `seconds`는 기본 300초, 도구의 상한은 1800초다. 실제 수집기는 이 시간에 맞춰 각각 기동하며 연장이 필요하면 새 실행으로 남긴다.

실제 기동 전에는 [기존 외부 수집 절차](NGINX_EXTERNAL_DIAGNOSIS.md)와 [TCP 수집 절차](NGINX_EXTERNAL_BROWSER.md)를 따른다. AWS 계정 `483175530259`, 서울 Region, App `i-05d320e3a87200d5a`, PLG `i-05c76ccb2b53f9279`를 확인한다. 방어 SHA·수집 소스 SHA·디스크 사용률도 기록한다. 설치된 파일이 다르면 먼저 소스를 비교하고 필요한 관측 파일만 배치한다. Nginx·방화벽·SG·운영·V2 QA 설정은 바꾸지 않는다.

App의 기존 배치 위치는 `/opt/yeodam/ops/nginx-edge438`, TCP 도구는 `/opt/yeodam/ops/nginx-browser445`다. host는 `nginx-rehearsal.py start --run-id <captureId> --seconds 300 --expected-site-sha256 <실제SHA> --detailed-processes`를 사용한다. ENA 출력은 같은 host root의 `ena/`, normal은 별도 normalId로 `nginx-tcp-normal-monitor.py --run-id <normalId> --seconds 300`을 실행한다. host plan의 `normalRunIds`에는 이 normalId를 연결해 사후 archive가 포함하도록 한다. 수집기 실행은 기존처럼 systemd의 시간·CPU·메모리 상한 아래에서 수행한다. normal이 발급한 세션은 App 밖으로 옮기지 않는다.

Mac external은 `--mode probe --run-id <externalId> --seconds 300 --http-version 2 --confirm-target https://v1-staging.yeodam-2gether.com --output <새폴더> --execute`다. TCP는 기존 절차의 systemd 실행 형식에 captureId와 확인한 source IP를 전달한다. TCP 도구는 host의 `plan.json`이 있어야 시작하며 시간·10만 패킷·32MiB 상한을 유지한다. 기동 요청의 성공과 checkpoint 생성은 별도로 확인한다.

## 출처 필터와 수집 신선도 확인

단말에서 받은 서비스 응답의 x-request-id를 App의 원본 접근 로그에 연결해 `peer_addr`를 확인한다. X-Forwarded-For를 믿거나 Mac의 IP를 단말 IP로 대신하지 않는다. IP·TCP 필터 문자열은 root/private에서만 취급하고 공유 자료에는 넣지 않는다. 여러 HMAC 도구는 서로 다른 키를 쓰므로 sourceId 문자열을 직접 조인하지 않는다.

현재 필터를 확인한 뒤 세션 폴더에 아래 `source-binding.json`을 기록한다. 값은 예시가 아니라 이번 실행의 실제 값으로 채운다. filterSha256은 TCP checkpoint의 값과 일치해야 한다. `serverPeerMatched`·`tcpFilterVerified`는 운영자가 원본 대조 후 기록하는 확인값이며 도구가 IP를 자동 추론한 결과가 아니다.

```json
{
  "captureId": "manifest의 실제 captureId",
  "requestId": "확인한 실제 32자리 응답 ID",
  "confirmedAt": "실제 확인 시각과 timezone",
  "networkChangedSinceConfirmation": false,
  "serverPeerMatched": true,
  "tcpFilterVerified": true,
  "filterSha256": "TCP checkpoint의 실제 SHA256"
}
```

네트워크 전환·주소 변경이 알려지면 즉시 확인값을 무효화한다. 같은 LTE에서도 주소가 바뀔 수 있으므로 실패 후 필터가 여전히 해당 단말을 포함했는지 확인한다. 아직 HTTP 응답을 한 번도 받지 못해 출처를 못 찾으면 확인값을 만들지 않는다. 완전한 수집을 위해 방어를 해제하지 않는다.

준비 상태 검사는 **최신 checkpoint 사본**을 아래 경로로 가져온 뒤 실행한다. SSM 읽기 전용 조회로 원본을 갱신하며, 이전 사본의 updatedAt을 고쳐 쓰지 않는다. 원본→로컬 매핑은 다음과 같다.

| App/Mac 원본 | 세션 폴더 내 사본 |
| --- | --- |
| App `/var/lib/yeodam/nginx-rehearsal/<captureId>/checkpoint.json` | `host/checkpoint.json` |
| 같은 root의 `ena/checkpoint.json` | `ena/checkpoint.json` |
| App `/var/lib/yeodam/nginx-connections-426/<normalId>/checkpoint.json` | `normal/checkpoint.json` |
| Mac external 출력의 `checkpoint.json` | `external/checkpoint.json` |
| host root의 `tcp445/checkpoint.json` | `tcp/checkpoint.json` |

```bash
python3 tests/load/scripts/nginx-failure-evidence.py check \
  --directory /private/tmp/nginx-failure-452-session \
  --output /private/tmp/nginx-failure-452-session/readiness-01.json
```

host·ENA·TCP는 5초, normal·external은 10초 이내의 갱신, 실행 ID 일치, 오류 0, 종료되지 않은 상태가 필요하다. source 확인은 300초 이내이고 TCP 필터 SHA가 일치해야 한다. 실패 시 exit 2와 누락 사유를 남긴다. TCP 패킷 0·필터 미확인·ENA 기준 누락도 실패다. 따라서 실제 접속 전 준비 확인에는 해당 출처의 짧은 확인 요청 표본이 필요하며, 이번 로컬 구현 작업에서 사용자에게 재접속을 요청하지 않는다.

`serverSnapshotReady=true`도 단말 기록 보존이나 앞으로의 수집을 보증하지 않는다. 사본을 검사한 결과이며 검사 후 수집기가 멈출 수 있다. `phoneNavigationCaptureVerified`는 이 도구에서 항상 false로 남기고 실제 단말 확인 결과는 별도로 기록한다. 이 도구는 부하 컨트롤러를 대신하거나 부하 실행 권한을 주지 않는다. 실패한 probe 이력을 지우거나 정상 임계값을 늘려 준비 완료를 만들지 않는다.

## 첫 페이지와 인증 기록을 잃지 않기

실제 단말을 사용하는 다음 관측에서는 **접속 전** Network의 화면 이동 기록 보존을 켠다. Safari 검사 도구의 Preserve Log 등 실제 지원 옵션과 활성 상태를 확인하고, 기록의 가장 오래된 요청이 로그인 시작보다 앞서는지 확인한다. 지원하지 않거나 외부 인증 이동 후 기록이 사라지면 각 페이지를 나누어 저장하고 경계 시각을 적는다. 원격 검사창을 보았다는 사실만으로 파일에 저장됐다고 간주하지 않는다.

이번 격리 표본은 보존된 HAR의 정제·분석을 검증한다. 실제 iOS Web Inspector가 OAuth 전환을 보존하는지는 이번 작업에서 검증하지 않았다. 다음 실제 관측 전에는 authorize→provider→callback→exchange→map이 파일에 남는지 확인해야 한다. 다섯 항목이 있다고 해서 사용자가 본 화면·정확한 실패 시각·모든 재시도가 완전하게 보존됐다고 판단하지 않는다.

실패 시 새로고침·재로그인을 먼저 하지 않는다. 화면 오류, 전체 주소, 단말 네트워크와 시각을 private에 보존한다. URL query에 인증 코드가 포함되면 공유본에서 제거한다. 실제 동작 시각을 모르면 observedAt=null로 두고 채팅 수신 시각을 대신 넣지 않는다. 해당 실행의 관측 메모는 다음 형태로 `client/observation.json`에 둔다. 전체 원본 URL·화면은 별도 private에만 남긴다.

```json
{
  "reportedAt": "실제 보고 수신 시각과 timezone",
  "observedAt": null,
  "route": "/login",
  "network": "LTE-reported",
  "symptom": "로그인 버튼 이후 대기"
}
```

원본 HAR는 private 0700/0600으로 두고, 채팅·Git·공유 ZIP으로 보내지 않는다. 정제 도구는 서비스와 `accounts.kakao.com`·`kauth.kakao.com`의 origin·고정 경로·시각·상태·단계 시간만 보존한다. 임의 경로는 `[other]`, 쿼리·Cookie·헤더·본문·IP·다른 제삼자 URL은 제거한다. response header는 x-request-id만 허용한다.

```bash
node tests/load/scripts/sanitize-nginx-mobile-har.mjs \
  /private/tmp/nginx-failure-452-session/private/phone.har \
  /private/tmp/nginx-failure-452-session/client/har.json
```

`missingAuthPhases`가 있으면 인증 구간 공백을 먼저 보고한다. status 0·누락 timing·캐시·취소·browser-policy 오류를 TCP/TLS 장애와 구분한다. iOS HAR가 알려진 오류 코드를 내보내지 않으면 failureStage는 null로 남는다. Console 화면·오류 문자열은 원본에서 확인하며 문맥 없는 minified 문자를 원인으로 사용하지 않는다.

## 종료·내보내기·사후 대조

실패를 관측하면 실제 부하가 있는 경우 해당 발생기를 먼저 중지하고 수집기는 짧은 회복 구간까지 보존한다. 수집기 오류·디스크80%·시간/출력 상한에서는 확대하지 않는다. 모든 기동·중지는 manifest의 이번 ID로만 한다. 예전 UUID를 넣은 helper를 재사용하지 않는다.

- App host: `nginx-rehearsal.py stop --run-id <captureId>`로 STOP 요청.
- App ENA: host root의 `ena/STOP`, TCP: `tcp445/STOP` 생성.
- App normal: `/var/lib/yeodam/nginx-connections-426/<normalId>/STOP` 생성. 별도 normalId는 host stop만으로 종료되지 않는다.
- Mac external: 해당 출력의 `STOP` 생성. 자신의 프로세스 종료와 summary 확인.
- 종료된 summary/result의 시각·ID·오류를 확인한다. normal의 `credentialsRevoked=true`·cleanupErrors=[]도 확인한다. STOP 요청만으로 폐기 완료라고 쓰지 않는다.

기존 `fetch-nginx-rehearsal.py --run-id <captureId> --ops-directory /opt/yeodam/ops/nginx-edge438 --output <새증빙폴더>`로 App·PLG·CloudWatch를 가져온다. 실제 발생기 실행이 없으면 generator 포함 옵션을 넣지 않는다. 기존 export는 raw IP·tcpdump stderr·키를 제외한다. TCP 필터 HMAC은 실행마다 달라지므로 보존된 private 매핑 없이 다른 도구의 sourceId와 조인하지 않는다.

정제 App access는 세션의 `app/access.jsonl`, export의 `app/tcp445-packets.jsonl`·`app/tcp445-summary.json`은 각각 `tcp/packets.jsonl`·`tcp/summary.json`으로 사본을 둔다. SHA256과 run ID를 확인하며 원본 export는 유지한다. 각 collector의 summary도 해당 role 폴더에 보존한다. 복사·정제 실패는 파일을 만들거나 수치를 0으로 채워 숨기지 않는다.

```bash
python3 tests/load/scripts/nginx-failure-evidence.py analyze \
  --directory /private/tmp/nginx-failure-452-session \
  --output /private/tmp/nginx-failure-452-session/analysis-01.json
```

입력 파일 SHA256, 인증 구간 누락, 요청 ID의 정확한 매칭 수, TCP 패턴과 수집 공백을 남긴다. 시간이나 IP가 비슷하다는 이유만으로 매칭하지 않는다. 요청 ID가 없는 클라이언트 실패의 serverMatches=0은 요청 손실 수가 아니다. SYN·SYN-ACK 관측도 ACK 순서·TLS 성공의 증명이 아니며 이미 재사용하던 연결에는 초기 SYN이 없을 수 있다. analyzer는 항상 rootCause=undetermined로 두고 최종 원인은 아래 증거를 사람이 대조해 판단한다.

| 실제 증거 | 좁힐 범위 | 단정하지 않을 것 |
| --- | --- | --- |
| 단말 DNS 오류 | 이름 조회 단계 | 통신사 DNS가 원인이라는 확정 |
| 확인한 출처·시각의 SYN만 관측 | TCP 연결 왕복·재전송·방어 카운터 | 수집 범위 밖 응답까지 없었다는 주장 |
| TCP 성립 자료와 단말 TLS 오류 | TLS 협상/인증서 단계 | TCP 플래그만으로 TLS 실패 판정 |
| 단말 HTTP 응답·동일 요청 ID | Nginx·FE·BE 처리 | HTTP200만으로 화면/로그인 성공 |
| authorize 뒤 callback까지 공백 | 외부 인증·사용자 조작·복귀 연결 | 공백 전체를 기술적 지연으로 계산 |
| 올바른 TCP 필터/시간을 확인 못 함 | 관측 공백 유지 | HTTP 로그가 없으니 서버 미도착이라는 주장 |

근거가 빠진 실패는 추가 측정 대상으로 남기되 같은 정상 로그인 시험을 자동 반복하지 않는다. 새로운 검증은 남은 실패 단계에 필요한 자료가 무엇인지 먼저 정한 뒤 진행한다.
