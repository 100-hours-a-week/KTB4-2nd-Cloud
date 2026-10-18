# V2 요청 경로 전환용 Nginx 브리지

기존 주소로 접속한 사용자도 Nginx → ALB → V2로 전달하기 위한 **스테이징 준비 설정**이다. 이 디렉터리를 병합해도 배포, DNS 변경, AWS 접근과 부하 발생은 실행되지 않는다. 기존 `nginx/yeodam.conf`와 연결하지 않는다. 별도 시험 Host와 실행 위치는 아직 선정 전이다.

## 생성 전에 확인할 조건

`config.example.json`의 주소와 경로는 예시다. 해당 DNS/ALB/인증서가 생성돼 있다고 가정하지 않는다. 기존 파일을 복사해 수정하고 변경 전후 실행 이미지 Digest를 고정한다.

| readiness 항목 | 확인하고 evidence 참조를 기록할 내용 |
| --- | --- |
| writer_and_schema | V1/V2가 사용할 단일 Writer, 실제 Flyway 이력과 구형 앱 호환성 |
| session_csrf_refresh | 전환 전에 로그인한 사용자의 Cookie/JWT 검증, 세션·CSRF·Refresh 유지 |
| legacy_upload_and_assets | 기존 탭의 다음 업로드 배치, Polling, 정적 자산과 Server Action 호환 |
| pending_jobs | 전환 전 접수한 작업의 원본과 결과 수신·중복 방지·완료 |
| host_tls_oauth | 동일 사용자 Host의 인증서·Origin·OAuth/Callback·SSR API, ALB 경유 TLS와 Timeout |
| rollback_with_current_writer | ALB 직접 유입과 Nginx 잔류 유입 모두의 복구, 최신 Writer 유지 |

미확인은 `null`로 남긴다. 확인 후 비밀 값 대신 로컬 evidence 경로/승인 기록 참조 문자열을 넣는다. `true`나 빈 문자열은 허용하지 않는다. 도구는 이 수동 기록을 검사할 뿐 실제 인증이나 DB 상태를 확인하지 않는다. V1 시험 이미지와 운영 이미지의 인증/업로드 방식이 같다고 가정하지 않는다.

## 로컬 검사

실행 위치는 이 Branch의 Cloud 저장소 루트다. Python 3, OpenSSL, Docker가 필요하다. 다음 검사는 서버를 호출하지 않는다.

```bash
python3 -m unittest discover -s tests/ci -p 'test_request_path_bridge.py'
bash tests/ci/check-request-path-bridge.sh
```

두 번째 명령은 임시 자체 서명 인증서로 `nginx -t`와 로컬 모의 Upstream 시험을 수행한다. Nginx 1.31.2의 고정 Digest 컨테이너에 `--network none`을 적용하고 Host Port를 공개하지 않는다. Host·POST Method·인코딩된 URI/Query·TLS SNI·시험용 Cookie/Authorization 전달, 위조된 전달 헤더의 교체, 다른 Host의 421 거절을 확인한다. 32KiB 응답이 일부 전달된 뒤 reload하고 전체 바이트를 비교한다. 컨테이너와 임시 파일은 종료 시 삭제한다.

모의 Upstream은 컨테이너의 loopback에 있고 같은 이름의 named upstream으로 ALB DNS 조회를 대체한다. **따라서 실제 DNS 재해석·ALB·업무 POST 본문 저장·세션·비동기 작업 보존을 검증한 것은 아니다.** 실제 배포할 Nginx 버전도 실행 전에 따로 확인한다. 이미지가 캐시에 없으면 Docker가 이미지를 내려받는다.

개별 설정을 확인하려면:

```bash
cp deploy/request-path-bridge/config.example.json /private/tmp/yeodam-bridge.json
# 예시값을 시험 대상으로 수정하고 readiness evidence를 기록한다.
python3 scripts/render-request-path-bridge.py check --config /private/tmp/yeodam-bridge.json
```

미확인 항목이 있으면 JSON의 `pending` 목록과 종료 코드 2를 반환한다. 값이나 필드가 잘못되면 코드 1이다. 모두 수동 확인됐으면 코드 0이지만 서버 검증 성공을 의미하지 않는다.

```bash
python3 scripts/render-request-path-bridge.py render \
  --config /private/tmp/yeodam-bridge.json \
  --release "$(git rev-parse HEAD)" \
  --output /private/tmp/yeodam-bridge.conf
```

미확인이 있으면 생성하지 않는다. **로컬 구문 검사만** 할 때는 `--allow-unverified`로 생성할 수 있으며 결과에 `UNVERIFIED / LOCAL CHECK ONLY`가 표시된다. 출력 파일이 이미 있으면 덮어쓰지 않는다. 실패 원인을 확인한 뒤 새 출력 경로를 사용한다. `/etc/nginx`로 복사하거나 reload하는 명령은 이번 준비 작업에 포함하지 않는다.

## 설정의 동작과 적용 전 점검

- 사용자 Host는 명시적으로 고정하고 다른 Host는 421로 거절한다. HTTP는 같은 Host의 HTTPS로 308 전달하며 ACME 경로를 유지한다. 실제 인증서 갱신 방식과 IPv6 listener/AAAA 여부도 적용 전에 확인한다.
- 연결은 별도의 서울 ALB DNS를 사용한다. 공개 사용자 Host를 연결 대상으로 사용해 순환하지 않도록 하고 ALB IP도 고정하지 않는다. 변수 `proxy_pass`와 명시적 Resolver로 재조회하며 **VPC에서 신뢰할 수 있는 Resolver인지 실제 확인**한다. IP만 다른 값으로 적었다고 루프와 잘못된 DNS 응답이 자동 탐지되는 것은 아니다.
- `upstream_tls_name`은 ALB가 제시하는 인증서 이름이다. 연결용 `*.elb.amazonaws.com` 주소와 구분하고 SNI와 CA 검증을 켠다. DNS를 ALB로 직접 전환할 때는 ALB 인증서가 **사용자 Host도** 포함해야 한다. 브리지 구간 TLS 이름만 맞췄다고 이 조건이 충족되지 않는다.
- Host·원본 URI/Query·Cookie·Authorization·본문을 전달한다. Cookie Domain/Path와 Location을 자동 수정하지 않는다. 앱의 공개 Origin, OAuth, SSR API 주소를 같은 사용자 Host로 맞춘다. 본문 상한과 각 Timeout은 예시이며 ALB idle timeout, BE/AI·클라이언트 상한과 함께 확인한다.
- 직접 Client가 Nginx에 접속하는 것을 전제로 X-Forwarded-For/Host/Proto/Port와 X-Request-ID를 새로 설정한다. 앞단 Proxy나 real_ip 모듈이 있다면 신뢰 경계를 먼저 재검토한다. WebSocket Upgrade는 지원하지 않으며 필요할 경우 별도 계약으로 추가한다.
- `proxy_next_upstream off`로 실패한 POST의 임의 재전송을 막는다. 요청 버퍼링은 영속 Queue나 손실 방지 보장이 아니다. 본문 임시 디스크 예산과 유휴/연결 상한도 기존 설정과 대조한다.
- Access Log는 정규화한 경로·Nginx 생성 요청 ID·상태·시간·Upstream 주소를 남긴다. Query, Cookie, Authorization, 본문과 Client IP는 기록하지 않는다. 기존 Error Log는 원본 요청 URI를 포함할 수 있으므로 수집·공유할 때 별도 마스킹이 필요하다. Client에 별도 요청 ID 헤더를 반환하지 않으므로 현재 앱의 로그 식별자와 연결 여부도 확인한다.

파일은 `http {}` 안에서 include하는 조각이다. 기존 site와 동시에 같은 listen/server_name을 활성화하지 않는다. 실제 설정의 include 순서, 기본 Server, `real_ip` 상속, 인증서와 ACME, 로그 디렉터리 권한을 `nginx -T`로 점검한다. Nginx와 ALB 양쪽 접근 제한·보안 그룹 및 Header 보존 설정도 실제 Host에서 확인한다.

## 실제 리허설 순서 — 아직 실행하지 않음

1. DB 시험 담당과 공유 자원 사용을 조율한다. 병렬로 시험하려면 별도 Host와 독립 Nginx 실행 위치부터 정한다. 별도 V2 QA DB에 연결한 시험은 전달 기능 검증으로만 기록한다.
2. 현재 `nginx -T/-v`, DNS A/AAAA·TTL, ALB 인증서/Listener/Rule/대상 상태·idle timeout, FE/BE Digest와 실제 환경을 읽고 readiness evidence를 채운다. 기존 서비스 상태·설정 백업과 전체 복구 경로를 확보한다.
3. 같은 Host·고정 이미지·최종 Writer로 로그인/조회/쓰기와 진행 업로드 기준선을 만든다. 사용자가 실행할 지속 부하의 RPS·시간·요청별 ID·허용 지연을 먼저 고정한다. 일반 여행 생성에 Probe 전용 멱등 재시도를 적용하지 않는다.
4. 독립 검증 후 기존 Nginx에 브리지를 적용할 때는 구문 확인 후 graceful reload한다. 기존 Worker가 처리 중인 HTTP 요청과 V1 비동기 작업은 각각 끝까지 관측한다. 새 Worker는 V2로 전달하지만 **기존 탭의 다음 요청까지 V1에 붙여 주지는 않는다**. 호환성 해결 없이 전체 유입을 넘기지 않는다.
5. DNS를 ALB로 전환한다. 이전 주소로 오는 Nginx 유입과 ALB 직접 유입을 나눠 보고, TTL만으로 기존 유입/열린 연결 소멸을 판정하지 않는다. DNS 캐시 기간 중 두 경로를 유지한다.
6. 성공 응답뿐 아니라 DB·S3·접수/처리/결과 기록을 대조한다. HTTP 실패·미확정 응답·중복·누락·미실행 부하·지연을 구분한다. 진행 요청/작업과 기존 주소 유입이 끝난 것을 확인하기 전 V1을 종료하지 않는다.
7. 실패하면 신규 확대를 멈추고 기록을 보존한다. DNS 원복은 즉시 동작하지 않으므로 ALB 직접 유입도 처리할 복구 수단이 필요하다. 전환 후 쓰기가 발생한 DB를 옛 DB로 단순 원복하지 않는다. 호환 V1도 현재 Writer를 사용하거나 Forward Fix를 선택한다.

Nginx reload 동작은 [공식 제어 문서](https://nginx.org/en/docs/control.html), 변수 proxy_pass·SNI·재시도는 [proxy 모듈](https://nginx.org/en/docs/http/ngx_http_proxy_module.html)을 기준으로 한다. DNS 변경은 [Route 53 지침](https://docs.aws.amazon.com/Route53/latest/DeveloperGuide/best-practices-dns.html)을 함께 따른다. 이 준비 코드의 통과를 운영 무손실 전환의 증거로 쓰지 않는다.
