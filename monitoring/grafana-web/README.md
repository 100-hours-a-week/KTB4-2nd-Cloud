# Grafana HTTPS 접근

운영 `https://grafana.yeodam-2gether.com`, 스테이징 `https://grafana-staging.yeodam-2gether.com`.
각 환경 기존 Public ALB의 Host 규칙으로 Private PLG의 Grafana만 연결한다. 이 구성은 Grafana 자체 계정 로그인이며 AWS SSO나 GitHub OAuth를 연결한 구성이 아니다.

## 경로와 인증

- ALB 규칙1: Grafana 도메인의 `/metrics`, `/metrics/*`는404. 규칙2: 해당 도메인의 모든 나머지 경로는 Grafana로 전달한다. 기존 `/api/*` 규칙20보다 먼저 처리한다.
- ACM의 별도 인증서를 기존443 Listener에 추가하고 Route53 Alias를 만든다. 기존 앱 도메인·기본 인증서는 유지한다.
- PLG의3000은 ALB SG 출발지만 허용한다. Prometheus9090/Loki3100 수집 경로는 외부 ALB에 연결하지 않는다.
- Grafana `root_url`은 환경별 HTTPS 주소, 쿠키는 Secure/HttpOnly이며 SameSite=Lax다. 익명 접속·셀프 가입·일반 사용자 조직 생성은 차단한다.
- 서버 관리자는 기존admin 계정, 팀원은 개인별Viewer로 시작한다. Viewer도 조직의 데이터 소스를 조회할 수 있다. 팀원별 데이터 격리나 세부 데이터 소스 RBAC를 제공한다는 의미가 아니다.

## Terraform 준비와 적용

Cloud 저장소 루트에서 실행한다. AWS 계정483175530259, Region ap-northeast-2인지 STS로 확인한다. 환경별 backend.hcl은 기존 State 버킷을 사용하고 Git에 올리지 않는다.

```sh
export AWS_PROFILE=yeodam-admin
aws sts get-caller-identity
terraform -chdir=terraform/v2/staging init -backend-config=backend.hcl
terraform -chdir=terraform/v2/staging fmt -check
terraform -chdir=terraform/v2/staging validate
terraform -chdir=terraform/v2/staging plan -out=/private/tmp/staging-grafana.tfplan
```

먼저 전체 Plan을 확인한다. 기존 리허설 변수 파일을 생략하면 발생기·리허설 ECS 삭제가 표시될 수 있으므로 전체 Plan을 그대로 적용하지 않는다. 다른 작업이 동시에 진행되는 기존 State에서 이번 접속 자원만 적용할 때는 아래처럼 범위를 한정하고, 저장된 Plan의 변경 목록이 Grafana 관련 자원뿐인지 반드시 확인한다.

```sh
terraform -chdir=terraform/v2/staging plan -target=module.grafana_web -out=/private/tmp/staging-grafana-web.tfplan
terraform -chdir=terraform/v2/staging apply /private/tmp/staging-grafana-web.tfplan
```

인증서 검증·Route53 전파가 필요하다. ACM이 PENDING_VALIDATION이면 CNAME과 authoritative DNS를 먼저 확인한다. PLG3000이 아직 루프백 바인딩이면 Target이 unhealthy인 것이 예상되며 다음 Grafana 설정 적용 후 healthy가 되어야 한다. Targeted Apply는 전체 정합성 확인을 대체하지 않는다. 운영은 production 디렉터리로 바꿔 스테이징 검증 후 수행한다.

## 기존 PLG에 설정 적용

검토한 해당 환경 `plg-compose.yml`과 이 폴더의install.py를 SSM으로 대상 PLG에 전달한다. 파일에 비밀번호를 넣지 않는다. 대상 인스턴스에서root로 실행한다.

```sh
sudo python3 /run/grafana-web-install.py --environment staging --compose-source /run/plg-compose-next.yml
```

도구는 인스턴스ID·Compose 프로젝트·이미지·기존 관리자 Secret 길이를 점검하고 이전 Compose와 알림 규칙을root 전용 디렉터리에 백업한다. Grafana만 `up -d --no-deps --pull never`로 재생성하며 Prometheus/Loki를 재시작하지 않는다. DB 볼륨을 유지해 기존 계정·대시보드·알림은 보존한다. 기동 실패 시 이전 Compose로 자동 복구한다. 알림은 기존 조건·기간·활성 상태를 보존하고 해당 환경의dashboard annotation만 변경한다. 링크 갱신 중 실패하면 백업과 실제 규칙을 대조하고 도구를 다시 실행한다. 시험 장애 알림을 생성하거나 전송하지 않는다.

운영의 기존 `observe-core`, `observe-alerts` SSM Document와 스테이징의 기존alerting Document도 새 소스를 담도록 Terraform 갱신한다. 설치/activate 전체를 다시 실행할 필요는 없다. Localhost Basic 인증을 사용하는 기존 관리 도구는 유지된다. Secure 쿠키이므로 평상시 브라우저 로그인은 HTTPS 도메인을 사용한다.

## 조회·권한 검증

대상 PLG에서 다음 도구를root로 실행한다. 외부 HTTPS URL을 통해 로그인·대시보드·Prometheus 쿼리를 확인하므로 NAT443 및 DNS가 필요하다.

```sh
sudo python3 /run/grafana-web-verify.py --environment staging
```

확인 항목: 비로그인Dashboard/Admin API401, 외부metrics404, 로그인실패401, 임시Viewer 로그인/대시보드/쿼리200, Viewer 관리자API403, Secure/HttpOnly 쿠키. 임시Viewer는finally에서 삭제한다. 도구가 강제종료되어 계정이 남으면 Server administration의 `plg375-check-` 계정을 확인해 제거한다. 요청 몇 건의 접속 검증이며 부하·가용성 시험은 아니다.

브라우저에서도 로그인 화면과 정상 대시보드를 확인한다. Discord 규칙의dashboard annotation이 새 환경별URL인지 API로 확인하고 기존 수집기ID·기동 시각 및 up 시계열을 전후 비교한다. 외부알림 테스트는 별도 허용 없이 발송하지 않는다.

## 팀원 계정 추가와 회수

1. 관리자가 해당 환경 HTTPS 주소에서admin으로 로그인한다. 관리자 비밀번호는 각 환경 Secrets Manager `yeodam/v2/{환경}/plg/grafana-admin-password`에서 권한 있는 담당자만 조회한다. 채팅·Git에 복사하지 않는다.
2. Server administration → Users에서 팀원별 아이디로 사용자를 만든다. 초기 비밀번호는 개인에게 안전하게 전달하고 사용자가 로그인 후 변경한다. SMTP는 구성하지 않았으므로 메일 초대가 자동 발송된다고 안내하지 않는다.
3. Organization의Users에서 Viewer인지 확인한다. 대시보드 수정이 필요한 담당자만Editor로 변경하고 Server Admin은 운영 담당자에게만 부여한다.
4. 운영·스테이징은 서로 다른Grafana 사용자DB다. 두 곳을 사용하면 각 환경에 계정을 만든다. 퇴장 시 두 환경 계정을 각각Disable하고 기존 세션을회수한다.

실제 팀원 아이디는 사용자가 지정한다. 이 작업의 검증용 계정을 팀 공용 계정으로 남겨두지 않는다.

## 원복

외부 접속을 먼저 끊으려면 Grafana Host 규칙2를 제거하거나404 고정응답으로 바꾸고, 해당 DNS를 제거한다. Terraform 코드에서도 같은 변경을 반영해 재생성을 막는다. 기존 앱 규칙을 삭제하지 않는다.

Grafana 설정은 install.py가 출력한 백업의 `plg-compose.yml`을 /opt/yeodam/plg/plg-compose.yml에 복원한 뒤 Grafana만 재생성한다. 최초 백업은 루프백 바인딩이어서 기존 SSM 접속으로 돌아간다. 백업 alert-rules.json을 현재 UID별 규칙과 비교해 annotation만 복구한다. 데이터 볼륨·사용자 DB·Prometheus/Loki 데이터를 삭제하지 않는다.

## 참고

- https://docs.aws.amazon.com/elasticloadbalancing/latest/application/listener-rules.html
- https://grafana.com/docs/grafana/latest/setup-grafana/configure-security/configure-security-hardening/
- https://grafana.com/docs/grafana/latest/administration/user-management/server-user-management/
