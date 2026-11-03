# V1 사용자별 조회 제한으로 전환하기 위한 준비

## 10/29 실제 적용 결과

기존 Nginx App의 BE를 검증된 V1 이미지로 교체하고 사용자별5r/s·burst30을 활성화했다. 공개 경로의 IP 보조 한도는25r/s·burst30으로 적용했다. 생성1r/s·burst5, Keepalive15초, Worker·FD·TCP 보호와 FE/DB/다른 컨테이너는 유지했다. 원래 site SHA `bce9ee92...`에서 최종 `0de6143f...`로 조회 rate만 변경했으며 임시 nonce 로그 필드는 제거했다.

| 시험 | 같은 IP의 부하 결과 | 별도 정상 계정 결과 |
| --- | --- | --- |
| 인증20RPS·40초 | 200230건 / BE 사용자429570건 | 같은 IP40/40, 다른 IP40/40 성공 |
| 무인증20RPS·40초 | 401800건 | 같은 IP40/40, 다른 IP40/40 성공 |
| 같은 계정의2세션 각4RPS·40초 | 200230건 / BE 사용자42990건 | 다른 계정40/40 성공 |
| 무인증60RPS·15초 | 401404건 / Nginx IP429496건 | 다른 IP15/15 성공 |

3,035개 시험 요청의 nonce·서버 요청ID·상태·경로·upstream을 대조했다. 별도 정상 요청215건은 모두200, 주입 누락·5xx0이다. App/Loki3,705건의 요청ID 다중집합이 일치했고 1초 호스트1,568표본의 오류는0이었다. 초기 CPU 가드 중단과 제어 조회가 겹친 첫 미완주는 정상 결과에 합치지 않았다.

25r/s는 처리량 최대값이나 모든 공유망의 수용량이 아니다. 루프백 인증30RPS+정상1RPS를20초 완주한 관측과, 기존 문제가 확인된20RPS+정상조회가 공유할 여유를 기준으로30보다 낮게 선택했다. CPU 순간 상승과40RPS 단계 중단을 보존했으며 최대 용량으로 해석하지 않는다. 실제 공개 경로의 인증20RPS CPU 평균은26.641%, 단일1초 최대73.869%였다. CPU 가드는0.5초 표본2회 연속70%로 별도 판정한다. 한 IP의 합산 유입이 새 비상 한도까지 소모하면 정상 사용자도 여전히 제한될 수 있다.

## 재배포 시 프로필 유지

기존 보호를 가진 기본 렌더러는 계속5r/s를 생성한다. 새 프로필은 BE의 검증된 Digest와 실제 활성화 확인 뒤 명시적으로 선택한다. `nginx_user_read_profile.py`의 값은 이번 측정의25r/s·burst30이며, 기본값을 조용히 완화하지 않는다.

Cloud의 새 릴리스가 설치된 App SSM에서 다음 옵션을 사용한다. image-env의 BE 참조는 이 문서의 검증된 V1 Tag/Digest여야 하며 기존 FE 참조는 그대로 유지한다.

```bash
sudo /opt/yeodam/releases/<Cloud-commit>/scripts/deploy-v1-staging-source.sh \
  --scope app --release-dir /opt/yeodam/releases/<Cloud-commit> \
  --image-env /etc/yeodam/v1-source-images.env --user-read-limit
```

이 옵션은 App만 허용하며 Digest가 다르면 배포 전에 실패한다. 사용자 제한 환경을 설정하고 healthy BE의 실제 활성화 플래그를 확인한 뒤 Nginx25r/s를 렌더링한다. 활성화 확인에서는 비밀 환경변수 전체를 출력하지 않는다. `--user-read-limit` 없이 호출하면 기존5r/s 프로필이므로 이번 정책을 유지하는 재배포에는 해당 옵션이 필요하다. 운영/V2용 배포에는 사용하지 않는다.

현재 실제 적용은 BE만 교체한 전용 override이고 원본 릴리스 파일을 덮지 않았다. `/var/lib/yeodam/user-read459/`의 원본 이미지·환경은 root 전용으로 EC2 안에만 보존했다. 원복은 Nginx 원본을 먼저 복구한 후 이전 BE로 되돌린다. 원본을 임의로 가져오거나 Git에 넣지 않는다. 작업별 실행/복구 절차와 전체 원본·한국어 그래프는 Cloud `.local-docs/459-user-read-limit.md`와 `evidence/459-user-read-live-20261029/`에 보존한다.

## 10/28 준비 당시의 경계

Cloud #456에서 같은 IP의 무인증 20RPS와 정상 1RPS를 함께 보내자 정상 요청 30건 중 21건이 Nginx에서 거절됐다. 다른 IP의 정상 요청은 30건 모두 200이었다. 이 결과로 IP별 5r/s를 사용자별 한도로 대체할 필요를 확인했지만 더 큰 IP 상한의 용량은 측정하지 않았다.

대상은 Nginx 고도화 시험 App `i-05d320e3a87200d5a`다. DB 재측정용 복제 Source `i-03efe8652117623c2`와 V2 QA·운영은 제외한다. 기준 BE 소스는 `816d69ba`, 브랜치는 `279-be-v1-db-마이그레이션-시험-세팅`이다.

## BE와 Cloud의 경계

BE는 기존 JWT·SID·활성 사용자 인증과 인가를 통과한 사용자 ID로 GET/HEAD 조회 한도를 관리한다. 같은 사용자의 여러 세션은 한도를 공유한다. 헬스·CSRF·OAuth 예외와 쓰기는 기존대로다. 쿠키 존재·원본 JWT 문자열·클라이언트 사용자 ID를 Nginx 제한 키로 사용하지 않는다.

BE의 기본값은 비활성이며 활성화 설정은 다음과 같다. 5/30은 사용자별 비교를 위한 초기 후보다.

```ini
V1_USER_READ_RATE_LIMIT_ENABLED=true
V1_USER_READ_RATE_LIMIT_RPS=5
V1_USER_READ_RATE_LIMIT_BURST=30
V1_USER_READ_RATE_LIMIT_MAX_USERS=10000
```

BE 초과 응답은 `429`, `X-RateLimit-Scope: user-read`, `Retry-After`, JSON message `READ_RATE_LIMIT_EXCEEDED`다. 상태 공간 부족은 `503 / READ_RATE_LIMIT_STORE_FULL`이다. Nginx access log의 upstream_status로 Nginx 직접 거절과 구분한다. 기존 요청 ID를 보존한다.

BE에 한도를 추가해도 Nginx의 기존 IP별 5r/s를 유지하면 공유 IP 문제는 남는다. 따라서 두 변경을 순서대로 적용해야 한다. 반대로 사용자별 제한은 인증 전 CPU·DB 비용을 보호하지 않는다. 보조 IP 보호를 임의로 제거하거나 쿠키만 보고 면제하지 않는다. 준비 PR #460 당시에는 실제 서버를 바꾸지 않았으며 위10/29 결과와 구분한다.

## 로컬 후보 작성 도구

`prepare-user-read-cutover.py`는 저장해 둔 스테이징 site 파일에서 조회 IP rate/burst 두 값만 바꾼 후보 파일을 만든다. 원본 SHA, 현재 조회·생성 정책과 도메인이 맞지 않으면 거절한다. 입력을 덮어쓰거나 AWS 접속·설치·reload를 하지 않는다. 입력·출력·기대 SHA·IP rate·IP burst를 모두 명시해야 한다.

IP rate/burst에는 기본값이 없다. 6r/s 이상·burst30 이상이라는 입력 경계는 기존보다 더 엄격하게 만드는 실수를 막는 검증이며, 해당 범위가 서버의 안전 용량이라는 뜻이 아니다. 실제 값을 선정하기 전에는 후보를 만들거나 배포하지 않는다. 테스트의 17/43은 문자열 교체 보존 검증용 수치이며 제안값이 아니다.

## 실제 적용 전 남은 조건

1. V1 BE 브랜치의 테스트·리뷰 후 `rehearsal=true`로 이미지를 발행하고 SHA/Digest를 기록한다. V2 Manifest나 운영 이미지 알림을 보내지 않는다.
2. 기존 V1 App의 현재 이미지·환경·Nginx 파일과 SHA를 보관한다. BE 사용자별 제한이 실제로 활성화됐는지 저율 요청과 제한 경계로 먼저 확인한다.
3. 발생기를 사용할 수 있을 때 인증 전 요청 처리 비용과 정상 다중 사용자의 합산을 확인해 보조 IP 상한을 정한다. 준비 당시 미선정이었으며10/29 위 조건으로25r/s를 선택했다.
4. 후보 Diff에서 조회 rate/burst 외 변경이 없는지 확인하고, 현재 서버의 `nginx -t`와 graceful reload·원복 절차를 거친다. 생성·업로드·TCP·Keepalive·timeout은 유지한다.
5. 기존 #456과 같이 같은 IP 부하와 별도 정상 계정, 다른 IP 대조군을 분리해 시험한다. 무인증 부하뿐 아니라 인증된 과다 요청 사용자도 포함한다. 정상 요청의 예상치 못한 429, 공격 계정 차단, 401/429/503·upstream·요청 ID와 CPU/DB 비용을 대조한다.

원복은 보조 IP 보호를 기존 설정으로 복구한 뒤 BE 사용자 제한을 끄거나 이전 이미지로 되돌린다. IP 보호가 빠진 상태에서 BE 제한만 먼저 끄지 않는다. 준비 완료와 최종 실측 완료를 구분한다.

기본 `render-v1-staging-nginx.py`는 기존 IP별5r/s를 유지한다. 선택 프로필에서는 `--user-read-limit-enabled --backend-digest <검증Digest>`로만25r/s를 생성한다. staging 배포 스크립트는 실제 BE 활성화 확인 뒤 이 인자를 전달한다. 범용 후보 생성기는 계속 자동 배포와 분리한다.
