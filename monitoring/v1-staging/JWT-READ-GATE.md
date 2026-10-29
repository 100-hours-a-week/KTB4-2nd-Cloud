# V1 조회 JWT 사전 검증과 제한 경로 분리

기존 Nginx 스테이징 App 전용이다. 정상 JWT 요청과 무인증 요청이 IP 25 예산을 공유하지 않게 한다. **#468 준비 상태에서는 실제 정책을 전환하거나 부하를 실행하지 않았다.** 사용자가 발생기를 다른 시험에서 사용 중이므로 실제 활성화와 부하는 명시적 재개 확인 뒤 진행한다.

## 제한을 적용하는 순서

```text
기존 TCP 보호 → GET/HEAD 조회 진입
  → njs: accessToken 쿠키의 HS256 서명·exp/nbf·issuer·audience·sub/SID 형식 검증
      ├─ 서명 검증 성공 → 사용자 ID별 Nginx5r/s·burst30 → 기존 BE 인증 → 조회
      └─ 없음/위조/만료 → 별도 IP 25r/s·burst30 → 401 또는 초과429, BE 전달 없음
```

유효한 서명의 사용자 경로에는 공통 IP 25를 적용하지 않는다. 두 zone은 독립이다. 알고리즘은HS256으로 고정하며 요청의alg/kid/외부 헤더로 키를 선택하지 않는다. 동일 계정의 여러 SID는 `u:<sub>` 하나를 공유한다. 클라이언트가 보낸 사용자 헤더는 제한 키가 아니다.

사전 검증은 로그인 세션 인증을 대체하지 않는다. 로그아웃·탈퇴했지만 서명/기간이 유효한 토큰은 해당 사용자 예산을 소모할 수 있고, BE의 기존 SID·사용자 상태 검사에서 거절된다. 다른 사용자의 예산을 사용하지는 못한다. 이를 완전히 제거하려면 세션 상태의 앞단 조회·동기화가 추가로 필요하지만 이번 범위에는 넣지 않았다. 기존 BE 사용자별 제한도 보조 보호로 유지한다.

기본 렌더링은 기존 정책을 유지한다. 명시적으로 선택할 때만 다음 옵션을 사용한다. 실제 설치·reload는 수행하지 않는 파일 생성 명령이다.

```bash
python3 scripts/render-v1-staging-nginx.py \
  --template nginx/yeodam.conf --output /tmp/v1-jwt-candidate.conf \
  --release <40자리-Cloud-Commit> --user-read-limit-enabled \
  --backend-digest sha256:3f1cb707caa276a7b834d92279d3738184f0b7c8b681e876544f0cc9612dc2b8 \
  --jwt-read-gate
```

일반 `limit_req`는 인증보다 먼저 실행된다. 따라서 기존 API location은 GET/HEAD만419 내부 전환으로 njs에 보내고, 서명 검증을 마친 뒤 별도 named location의 `limit_req`를 실행한다. 외부 redirect·추가 TCP proxy hop은 없다. 내부 변수는 전환 사이에 유지된다. `js_access` 등 최신 njs 전용 기능을 요구하지 않는다.

생성 exact location의 POST418 전환과1r/s·burst5는 유지한다. POST 업로드 본문은 njs에서 읽지 않는다. 보호 대상 GET/HEAD에 본문이 있으면413으로 거절해 njs의 불필요한 대용량 본문 버퍼링을 막는다. 기존 health·CSRF·OAuth 예외와 Keepalive15초·Worker/FD·TCP 설정은 그대로다.

## 버전과 비밀키

10/29 읽기 전용 점검에서 App의 nginx/nginx-common은1.24.0-2ubuntu7.19, njs 후보는libnginx-mod-http-js0.8.2-1ubuntu1이었다. 아직 App에 모듈을 설치하지 않았다. 로컬 Ubuntu 컨테이너는 이 버전을 고정하고 실제 njs로 검증한다.

V1은 대칭키HS256이다. 검증 키는 발급에도 쓸 수 있으므로 Nginx worker의 키 접근 범위가 추가된다는 한계가 있다. 키는 기존 App에서만 읽고 root0600 JSON으로 준비한다. `js_preload_object`로 master가 읽어 worker에 제공하고, Cookie·키·사용자 ID는 로그에 넣지 않는다. 실사용 키/토큰은 Mac·Git·PR로 전송하지 않는다. 설정/키/암호화 오류는503으로 닫고 IP 면제나 BE 전달로 대체하지 않는다.

Spring Security7.1.1의 실제 JwtTimestampValidator class에서 기본60초 오차를 확인했다. 앞단도같은60초를 사용한다. issuer는 실행 컨테이너의JWT_ISSUER, audience는고정 이미지의yeodam-api다. 임의의 JWT 설정 override가 있으면 동등성을 먼저 확인해야 한다. 사전 검증에는 캐시나 세션 DB 조회를 추가하지 않았다. 서명 검증 CPU는 실제 시험에서 측정할 항목이며 용량을 미리 주장하지 않는다.

## 호스트 준비와 실제 활성화 분리

실행 대상은 원본 App `i-05d320e3a87200d5a`의 root SSM이다. 복제 Source·V2 QA·운영은 거절한다. 실제 서버에 전달하는 source directory에는 이 저장소의 `monitoring/v1-staging` import 의존 파일과 `jwt-gate` 파일을 함께 둔다.

```bash
sudo python3 /opt/yeodam/ops/jwt-gate468/jwt-gate/prepare.py stage \
  --expected-site-sha <점검한-site-SHA256> \
  --expected-main-sha <점검한-main-SHA256>

sudo python3 /opt/yeodam/ops/jwt-gate468/jwt-gate/prepare.py status
```

`stage`는실행 BE Digest·healthy·기존 사용자 5/30 활성화를 확인한다. `/var/lib/yeodam/jwt-gate468/`에 원본/후보 site·스크립트·root전용 JWT JSON·민감값 없는 영수증을 보관한다. **패키지 설치·활성 site 변경·reload·HTTP 요청은 하지 않는다.** 기존 stage를 덮어쓰지 않는다. SHA가 달라지면 원인을 확인하고 새 준비 여부를 판단하며 기대값을 자동 갱신하지 않는다.

사용자 허락을 받은 실제 시험 창에서만, Account/Region/대상·다른 시험 충돌·수집기·원복을 확인한 뒤 진행한다.

1. `apt-get --simulate install --no-upgrade libnginx-mod-http-js=0.8.2-1ubuntu1`에서 Nginx 교체/삭제 등 예상 밖 변경이 없는지 확인한다. 예상과 다르면 중단한다.
2. 검토한 패키지만 설치하고 `/etc/nginx/modules-enabled`의 모듈 로드와 기존 설정의 `nginx -t`를 확인한다. 기존 Nginx 본체를 업그레이드하거나 다른 모듈을 추가하지 않는다.
3. 아래명령으로후보를적용한다. 원본 site/main과 staged코드 SHA·BE·JWT 설정이 변하면 거절한다. config0600을 설치하고 `nginx -t`와graceful reload한다. 실패 시 기존 site를 복원한다.

```bash
sudo python3 /opt/yeodam/ops/jwt-gate468/jwt-gate/prepare.py activate \
  --confirm-instance i-05d320e3a87200d5a
```

4. 실제 정상 세션의 조회·헬스·예외 경로를 저율 확인한 뒤 별도 동의한 부하를 실행한다. 활성화 명령 성공만으로 공유 IP 보호를 완료 처리하지 않는다.
5. 실패 시 아래명령으로이전IP 25+BE사용자 5정책을먼저복구한다. Nginx module은 설치돼 있어도 이site에서사용하지않게된다. BE 이미지는교체하지않는다.

```bash
sudo python3 /opt/yeodam/ops/jwt-gate468/jwt-gate/prepare.py rollback \
  --confirm-instance i-05d320e3a87200d5a
```

롤백도현재site가이번후보SHA이고main이원본일때만실행한다. 다른 작업이변경한설정을덮지않는다. stage원본/비밀설정은root경로에만있으며로컬로그수집대상에넣지않는다.

## 승인 후 실제 시험의 판정

수집을 먼저 확인하고 고유 UUID와 응답 요청 ID를 대조한다. Nginx `jwt_gate_result`는not_applicable/signature_valid/missing/malformed/bad_signature/expired/not_yet_valid/bad_claims/error의 유한 값만 기록한다. 원본 토큰이나sub/SID는 기록하지 않는다. X-Edge-RateLimit-Scope와upstream_status로앞단거절과BE응답을구분한다. BE의X-RateLimit-Scope:user-read를덮지않는다.

- 같은 IP의 무인증·위조 부하가25RPS를 넘을 때 다른 정상 계정1RPS가200인지 확인한다. 잘못된 토큰은 njs 검증 비용도 따로 측정한다.
- 유효한 토큰의 과다 계정과 정상 다른 계정을 같은 출처에서 실행한다. 과다 계정의 Nginx429와 정상200을 확인한다.
- 같은 계정의 여러SID는예산을공유하고다른 IP대조군도유지한다.
- 허용된정상요청은BE의기존인증을통과해야한다. 앞단서명성공을DB세션정상으로집계하지않는다.
- 앞단거절은upstream없음, 정상및기존세션거절은upstream있음으로구분한다. 클라이언트·App·Loki에서요청수를대조한다.
- 정상 429/5xx/timeout·CPU가드·스케줄누락·수집실패를성공으로숨기지않는다. 기존안전중단기준과IP정책원복을사용한다.

기존 TCP동시/신규연결보호는여전히출처IP를공유한다. 이작업은조회HTTP예산분리이며연결수용·TLS·회선포화까지사용자별로격리하는기능은아니다.

### 준비한 실제 시험 클라이언트

`tests/load/scripts/nginx-jwt-read-trial.py`는 실행 명령이 없으면 시작하지 않는다. 원본 App/발생기 고정·루트·DNS·경합 도구·합성 사용자11007/107/107의 비공개 pool·충분한 만료 시간을 확인한다. 10초 기준선 뒤20초씩unauth40/forged40/auth20/auth40/sameuser8을 개별 실행할 수 있다. 각 단계의 실행 여부는 직전 결과를 확인해 결정하고 자동 RPS 탐색은 하지 않는다.

실행에는 `--execute --approved-run <동일 trial UUID>`가 필요하다. 이는 사용자 승인을 대신하는 토큰이 아니라 실수로 시작하지 못하게 하는 추가 실행 확인이다. **이번 준비에서는 실행하거나 발생기에 설치하지 않았다.** 정상 응답은200·올바른앞단scope·32자리요청ID를요구하고, 과다/무인증역할의허용상태도분리한다. 정상실패·예상밖scope·1초초과·5xx·스케줄누락·App CPU0.5초표본2회연속70%에서중단한다. App은자체실패시#468rollback으로이전IP 25정책을복구한다. 발생기측실패와상대프로세스정리는시험제어자가함께확인해야한다. 측정 중SSMpolling은하지않는다.

## 근거

- [njs 내부 전환과 변수 유지](https://nginx.org/en/docs/njs/reference.html#r_internalRedirect)
- [njs 암호화 기능](https://nginx.org/en/docs/njs/reference.html#crypto)
- [Nginx 실행 단계](https://nginx.org/en/docs/dev/development_guide.html#http_phases)
- V1 e87ca5f3: JwtConfig·ActiveLoginSessionValidator·CookieAccessTokenResolver, 실제Spring Security7.1.1 JwtTimestampValidator class
