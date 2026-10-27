# HTTP 요청량과 신규 TLS 연결량 분리 측정

V1 스테이징의 요청 제한 뒤에서 BE가 보호되는 것과, HTTP 이전 TCP·TLS 단계가 정상인 것은 별도 판단이다. 기존 방어를 유지한 채 연결을 재사용하는 HTTP 부하와 매번 새 연결을 만드는 부하를 비교한다. 이전 강사 시험의 LTE 접속 실패 원인을 확정하는 시험은 아니다.

## 비교 조건

대상은 `https://v1-staging.yeodam-2gether.com` 하나다. 기존 발생기 EC2만 부하를 만든다. 인증 없는 `GET /api/trips/map`을 사용하므로 401·429는 예상 음성 응답이며 로그인 사용자 성공이 아니다. 여행 생성·업로드·AI 작업을 등록하지 않는다.

모든 단계의 TLS는 1.2, ALPN은 HTTP/1.1이다. 인증서 검증과 호스트 SNI를 유지하고, 단계 시작에 DNS를 한 번 조회한 IPv4 주소로 접속한다. DNS는 부하 연결 시간에서 분리한다. 클라이언트 티켓을 비활성화하고 명시적 TLS 세션을 전달하지 않으며, 각 연결의 `session_reused=false`를 확인한다. 브라우저의 HTTP/2·TLS1.3·TLS 세션 재개에 대한 결론은 내리지 않는다.

| 단계 | 부하 | 목적 |
| --- | --- | --- |
| reuse10a → fresh10a | 10 HTTP/s, 각 30초 | 같은 요청량에서 재사용과 새 full TLS 비교 |
| fresh10b → reuse10b | 위 조건, 순서 반대 | 순서 효과를 확인하는 두 번째 표본 |
| reuse100 → reuse450 | 100·450 HTTP/s, 각 30초 | 신규 연결을 적게 유지하고 HTTP 요청량 증가 |
| tls10 | 10 full TLS/s, 30초, HTTP 전송 없음 | HTTP 처리를 제외한 연결 성립 관측 |
| fresh20 | 최대 20 신규 연결/s, 30초 | 현재 출처별 신규 SYN 제한 근처의 경계 확인 |

각 단계는 같은 클라이언트 Worker 4개를 사용한다. 서버 Worker 수와는 다르다. 재사용 모드도 최초 연결·서버의 연결 종료 시 새 TLS가 발생하므로 `tlsCompleted`를 실제로 센다. 목표 RPS만으로 연결이 재사용됐다고 간주하지 않는다. TLS-only의 HTTP 없는 종료는 Nginx 오류나 HTTP 400 흔적으로 남을 수 있어 정상 서비스 요청 실패와 구분한다.

현재 nftables는 출처별 신규 SYN 20/s·burst40 및 동시 흐름128이다. 정책을 완화하거나 우회하지 않는다. 따라서 새 TLS 시험은 이 출처 한도 안의 관측이며 서버 전체 TLS 최대 처리량을 측정한 것이 아니다. SYN 제한·재전송은 TLS가 시작되기 전 TCP에 영향을 줄 수 있다. 차단 카운터와 실패 단계 없이 이를 TLS 암호화 CPU 병목으로 부르지 않는다.

## 실행과 수집

Cloud 저장소에서 `scripts/nginx-http-tls-load.py`와 기존 `nginx-tcp-capacity.py`를 발생기 `/opt/yeodam/ops/nginx-tls440`에 배치한다. 실행 호스트·대상·속도·시간은 코드에서 제한한다. `--execute` 없는 실행은 계획 확인만 한다.

```bash
python3 tests/load/scripts/nginx-http-tls-load.py \
  --mode reuse --rate 10 --seconds 30 --workers 4 \
  --confirm-target https://v1-staging.yeodam-2gether.com \
  --output /private/tmp/plan-only.json
```

실제 실행은 기존 스테이징 발생기 root에서만 가능하다. `run-nginx-http-tls-trials.py`는 #438과 같은 context 형식과 독립 외부 checkpoint를 사용한다. App/PLG 수집 도구는 기존 `/opt/yeodam/ops/nginx-edge438`을 재사용하며 서비스 설정은 바꾸지 않는다. 새 capture·normal·external UUID로 호스트 1초 수집, Worker별 CPU/FD, ENA, App 내부 합성 계정 정상 조회와 Mac 독립 새 연결 probe를 먼저 시작한다.

```bash
python3 tests/load/scripts/run-nginx-http-tls-trials.py \
  --context /private/tmp/yeodam-tls440/context.json \
  --external /private/tmp/yeodam-tls440/external \
  --output /private/tmp/yeodam-tls440/trials --execute
```

컨트롤러는 AWS 계정과 App/발생기 고정 ID를 확인한다. 외부 probe 실패·수집 오류·10초 이상 정체 시 부하 unit 중지를 요청한다. 각 단계 전후 host/ENA 5초·내부 probe 10초 신선도, 정상 조회 실패0, 메모리512MiB 이상·디스크80% 미만·5표본 연속 CPU95% 초과 없음, Nginx SHA 불변을 확인한다. 새 ENA 초과 증분이 있으면 확대하지 않는다. 호스트 가드는 단계 전후 검사이며 모든 자원 초과를 실시간으로 즉시 중단한다고 주장하지 않는다.

발생기는 stage당 systemd RuntimeMaxSec45·메모리512MiB·CPU150%로 제한된다. 첫 전송 실패·예상하지 않은 상태·세션 재개/프로토콜 불일치에는 클라이언트가 다음 요청을 중단한다. 재시도하지 않는다. 스케줄 누락·미시작 요청·queue 지연을 별도 기록해 목표 미달을 서버 처리량으로 오인하지 않는다. 중단·실패 자료도 보존하고 이후 단계를 임의로 강행하지 않는다.

## 해석과 마감

`tcpMs`는 DNS를 제외한 connect 경과, `tlsMs`는 TCP 완료 이후 TLS 완료까지다. 후자는 네트워크 RTT·서버 대기·암호화·클라이언트 스케줄링을 포함하므로 TLS 시간만으로 CPU 병목을 확정하지 않는다. HTTP 시간은 TLS 이후 응답 body를 소비할 때까지다. 클라이언트 프로세스 CPU와 대기열도 함께 기록한다.

TCP 지연/실패가 먼저 증가하면 SYN 차단·수용·재전송을 본다. TCP가 안정적이고 TLS만 증가하면 Worker별 CPU 및 독립 외부 TLS 지연과 대조한다. 같은 발생기만 실패하면 출처별 정책이나 발생기 한계를 먼저 분리한다. 독립 외부 probe가 정상이어도 LTE와 모든 사용자 경로가 정상이라고 확대하지 않는다.

종료 후 정상 세션 폐기·수집 STOP·회복을 확인한다. 아카이브 도구 `tls-generator`와 fetch의 `--include-tls-generator`로 화이트리스트에 있는 #440 결과·실행 소스만 SSM 터널로 가져와 파일 SHA를 대조한다. App·Loki request_id 다중집합, 수집 오류·빈 구간을 확인하고 한국어 그래프를 원본에서 생성한다. 최종 수치와 판정은 로컬 작업 기록에 남긴다.
