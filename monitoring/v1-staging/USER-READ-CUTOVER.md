# V1 사용자별 조회 제한으로 전환하기 위한 준비

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

BE에 한도를 추가해도 Nginx의 기존 IP별 5r/s를 유지하면 공유 IP 문제는 남는다. 따라서 두 변경을 순서대로 적용해야 한다. 반대로 사용자별 제한은 인증 전 CPU·DB 비용을 보호하지 않는다. 보조 IP 보호를 임의로 제거하거나 쿠키만 보고 면제하지 않는다. 이번 PR은 실제 서버의 어떤 한도도 바꾸지 않는다.

## 로컬 후보 작성 도구

`prepare-user-read-cutover.py`는 저장해 둔 스테이징 site 파일에서 조회 IP rate/burst 두 값만 바꾼 후보 파일을 만든다. 원본 SHA, 현재 조회·생성 정책과 도메인이 맞지 않으면 거절한다. 입력을 덮어쓰거나 AWS 접속·설치·reload를 하지 않는다. 입력·출력·기대 SHA·IP rate·IP burst를 모두 명시해야 한다.

IP rate/burst에는 기본값이 없다. 6r/s 이상·burst30 이상이라는 입력 경계는 기존보다 더 엄격하게 만드는 실수를 막는 검증이며, 해당 범위가 서버의 안전 용량이라는 뜻이 아니다. 실제 값을 선정하기 전에는 후보를 만들거나 배포하지 않는다. 테스트의 17/43은 문자열 교체 보존 검증용 수치이며 제안값이 아니다.

## 실제 적용 전 남은 조건

1. V1 BE 브랜치의 테스트·리뷰 후 `rehearsal=true`로 이미지를 발행하고 SHA/Digest를 기록한다. V2 Manifest나 운영 이미지 알림을 보내지 않는다.
2. 기존 V1 App의 현재 이미지·환경·Nginx 파일과 SHA를 보관한다. BE 사용자별 제한이 실제로 활성화됐는지 저율 요청과 제한 경계로 먼저 확인한다.
3. 발생기를 사용할 수 있을 때 인증 전 요청 처리 비용과 정상 다중 사용자의 합산을 확인해 보조 IP 상한을 정한다. 아직 어떤 수치도 확정하지 않았다.
4. 후보 Diff에서 조회 rate/burst 외 변경이 없는지 확인하고, 현재 서버의 `nginx -t`와 graceful reload·원복 절차를 거친다. 생성·업로드·TCP·Keepalive·timeout은 유지한다.
5. 기존 #456과 같이 같은 IP 부하와 별도 정상 계정, 다른 IP 대조군을 분리해 시험한다. 무인증 부하뿐 아니라 인증된 과다 요청 사용자도 포함한다. 정상 요청의 예상치 못한 429, 공격 계정 차단, 401/429/503·upstream·요청 ID와 CPU/DB 비용을 대조한다.

원복은 보조 IP 보호를 기존 설정으로 복구한 뒤 BE 사용자 제한을 끄거나 이전 이미지로 되돌린다. IP 보호가 빠진 상태에서 BE 제한만 먼저 끄지 않는다. 준비 완료와 최종 실측 완료를 구분한다.

현재 기본 `render-v1-staging-nginx.py`는 기존 검증된 IP별 5r/s를 계속 생성한다. 이후 실제 전환 때 기본 렌더러·설치·점검 도구도 확정 정책에 맞춰 함께 반영해야 한다. 이 준비 도구를 기존 배포의 자동 적용 경로에 연결하지 않았다.
