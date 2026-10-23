# V1 스테이징 Nginx 연결 관측

범위는 V1 스테이징 App 한 대다. 요청 제한, Keepalive, Worker 수, 연결 한도,
업로드 Timeout, DB 접속과 기존 경로 전환 프록시는 변경하지 않는다.

```text
Nginx stub_status (127.0.0.1:8086)
  → nginx-prometheus-exporter (127.0.0.1:9113)
  → 기존 Alloy, 15초 scrape
  → 스테이징 Prometheus remote write
  → Grafana V1 staging source

Nginx JSON → 기존 Alloy 파일 수집 → 스테이징 Loki
```

Nginx 공식 exporter 1.5.3을 Digest로 고정했다. 별도 Compose 프로젝트여서
기존 App/ProxySQL/Alloy Compose를 다시 배포하지 않는다. Host network는
루프백 상태 페이지 접근에 사용하며 exporter 자체도 루프백에만 listen한다.
64MiB·0.25 CPU는 사용 상한이며 고정 소비량이 아니다. 호스트 지표와 실제 사용량을
구분한다. 보안 그룹에 새 포트를 열지 않는다.

## 지표 해석

| 지표 | 해석 |
| --- | --- |
| Active | Waiting을 포함한 현재 Client 연결 |
| Reading | 요청 Header를 읽는 연결 |
| Writing | 응답을 쓰는 연결. Upstream 대기 원인을 단독으로 구분하지 못함 |
| Waiting | 다음 요청을 기다리는 유휴 Keepalive 연결 |
| accepted / handled | 누적 접수·처리 연결 수. rate로 비교하며 차이는 자원 한도 증거와 함께 해석 |
| up / nginx_up | Alloy→exporter와 exporter→Nginx 두 구간의 수집 성공 여부 |

Client 연결 수는 Worker의 전체 연결 슬롯 사용량이나 Upstream 소켓 수가 아니다.
단순히 Active를 `Worker 수 × worker_connections`로 나눠 정확한 포화율이라고
주장하지 않는다. Header 완료 전 실패는 완전한 요청 로그에 남지 않을 수 있어
오류 로그와 함께 본다.

상태 페이지 수집 요청도 Nginx 카운터에 포함된다. 15초 주기의 수집 자체가 만드는
연결·요청을 실제 사용자 부하와 구분한다. 상태 페이지 access_log는 끄고 공개 요청은
기존 JSON 로그로 센다. 이 작업은 TLS 협상 중 연결이나 프로세스별 FD 사용률까지
계측한 것이 아니다.

## 로그 필드

`connection`, `connection_requests`, `worker_pid`, `http_protocol`을 추가한다.
숫자처럼 보이는 앞의 세 값도 JSON에서는 문자열이다. 기존 요청 ID·경로 정규화와
민감 정보 제외 정책을 유지한다. 연결 ID는 무한히 유일한 사용자 ID가 아니므로
시간과 PID, 요청 ID를 함께 사용한다. HTTP/2는 한 TCP 연결에 여러 요청이 동시에
진행될 수 있다.

값들은 로그 본문에만 보존하고 Loki stream/Prometheus label로 승격하지 않는다.
적용 전 로그에는 새 필드가 없다. 스테이징 렌더러에도 같은 함수를 연결해 재배포로
필드가 사라지지 않게 했다. 운영 Nginx 공통 템플릿에는 적용하지 않는다.

## 설치와 원복

`nginx-status.conf`, `nginx-exporter-compose.yml`, `nginx_observability.py`,
`install-nginx-observability.py`를 App `/opt/yeodam/ops/nginx-observability/`에 전달한다.
명령은 **V1 스테이징 App에서** 실행한다.

```bash
sudo sha256sum /etc/nginx/sites-available/yeodam-v1-staging
sudo python3 /opt/yeodam/ops/nginx-observability/install-nginx-observability.py install \
  --expected-site-sha256 INSPECTED_SITE_SHA256
```

실제 설정을 점검한 SHA256을 넣는다. 이행기와 별도 Header 등을 보존하기 위해
전체 사이트를 템플릿으로 교체하지 않고 현재 log_format만 수정한다. Alloy도 현재
파일에 scrape만 추가한다. Docker bind mount inode를 유지하도록 파일 내용을
제자리에서 쓰고, 현재 Alloy 이미지로 validate한 뒤 HTTP reload한다. Nginx는
`nginx -t` 후 graceful reload한다. 정상 상태에서 Worker PID는 교체되지만 Master와
기존 앱 컨테이너는 유지된다.

백업은 `/var/backups/yeodam/nginx-observability/apply-*`에 권한 제한으로 저장한다.
설정은 비밀 Header를 포함할 수 있으므로 원문을 채팅·Git·검증 로그로 출력하지 않는다.
실패 시 이전 설정을 복구하고 추가 exporter 프로젝트만 내린다.

```bash
sudo python3 /opt/yeodam/ops/nginx-observability/install-nginx-observability.py rollback \
  --backup ACTUAL_BACKUP_DIRECTORY
```

로그, Alloy 위치 파일, DB·세션은 삭제하지 않는다. 원복은 당시 설정으로 되돌리므로
후속 변경이 있었다면 diff부터 확인한다. Grafana 파일은 PLG에서 별도로 백업·교체한다.
기존 `/opt/yeodam/plg/grafana/dashboards/v1-source.json`만 교체하며 자동 provisioning을
확인한다. 이 파일의 원복은 해당 백업 파일을 원래 위치에 복사하는 방식이다.

## 확인

App에서:

```bash
curl -fsS http://127.0.0.1:8086/stub_status
curl -fsS http://127.0.0.1:9113/metrics
sudo ss -lnt
```

PLG Prometheus에서는 `up{job="v1-nginx",environment="staging"}`와
`nginx_up{job="v1-nginx",environment="staging"}`가 모두1인지, 마지막 지표가
45초 이상 오래되지 않았는지 확인한다. `up=1`이더라도 `nginx_up=0`이면 exporter가
살아 있고 Nginx 상태 조회는 실패한 것이다. No data를0으로 간주하지 않는다.

공개 도메인의 `/stub_status`·`/metrics`가 상태 정보를 노출하지 않아야 한다.
다른 EC2에서 App8086/9113 연결이 불가능한지도 확인한다.

Grafana: https://grafana-staging.yeodam-2gether.com/d/yeodam-v1-source

연결 재사용 확인은 같은 curl 실행의 HTTP/1.1 헬스 GET2건을 보내 응답 ID와 로그를
대조한다. 연결 ID는 같고 connection_requests가1씩 증가해야 한다. 새 연결 모드나
HTTP/2·고부하 검증과는 구분한다.

## 정상 기준선으로 이어가기

관측 추가 후 Release/설정과 측정 시작·종료 UTC를 고정하고 로그인·조회·실제 업로드를
측정한다. 사용자 k6 실행 전에 토큰·데이터와 V1 Worker 상태를 확인한다. 여행 생성의
Mock/시험 프로필과 실제 AI 완료 시험은 구분한다. 이번 설치의 헬스 확인은 정상
사용자 기준선 측정이 아니다.

- 지연: 공개 요청의 성공률·p95, BE Upstream 시간, 오류 상태를 경로별로 비교
- 연결: Active/Reading/Writing/Waiting과 신규 연결 rate, 연결별 요청 수
- 자원: Host CPU·메모리·Root disk, 연결 부족 오류
- 사용자 결과: 원본 수·AI 완료·조회 결과를 별도 대조

기준선·대표 재현 부하에서 먼저 고갈되는 항목을 확인한 다음 방어 한 가지를 선정한다.
부하 명령은 사용자가 직접 실행한다.

## 공식 근거

- https://nginx.org/en/docs/http/ngx_http_stub_status_module.html
- https://nginx.org/en/docs/http/ngx_http_core_module.html#variables
- https://github.com/nginx/nginx-prometheus-exporter
