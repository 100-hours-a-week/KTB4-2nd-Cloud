# V2 스테이징 ALB와 HTTPS 라우팅

2026-10-07 기준 이 문서는 Issue #126의 Terraform 코드와 실제 계정 Plan 검증을 기록한다. #135에서 스테이징 전체와 함께 ALB, ACM 인증서, DNS 검증 레코드, FE/BE Target Group을 AWS에 적용했다. 공개 스테이징 도메인의 ALB Alias와 FE/BE ECS Service는 아직 없으므로 화면·API 요청과 Task 장애 전환은 검증하지 않았다.

## 요청이 이동하는 경로

```text
브라우저 → staging.yeodam-2gether.com → HTTPS ALB
                                       ├─ /api/health → FE Target Group :3000
                                       ├─ /api/*       → BE Target Group :8080
                                       └─ 그 외         → FE Target Group :3000
```

ALB는 요청을 받은 도메인만으로 FE와 BE를 구분하지 않는다. 443 Listener의 **경로 규칙**이 Target Group을 선택하고, 요청 경로를 고치지 않은 채 해당 Task로 전달한다. 따라서 브라우저가 스테이징 화면에서 `/api/trips`를 요청하면 주소는 `https://staging.yeodam-2gether.com/api/trips`가 되고 스테이징 ALB가 BE Target으로 보낸다. 운영 화면은 운영 도메인의 별도 ALB를 사용하게 된다. `/api` 접두사는 BE의 현재 `server.servlet.context-path=/api`와 일치하므로 이 라우팅만을 위해 BE API 경로를 바꾸지 않는다.

현재 FE는 `/api/health`를 제공한다. 이 경로는 일반적인 `/api/*` 규칙보다 높은 우선순위 10으로 FE에 전달하고, BE 규칙은 우선순위 20이다. Target Group의 Health Check도 각각 FE `/api/health`, BE `/api/actuator/health`를 직접 호출한다. 4단계 설계의 공통 `/health/ready`는 아직 현재 두 저장소의 구현 계약이 아니므로 이번 Terraform에 임의로 적용하지 않았다. 특히 BE Actuator Health에 DB/Redis 의존성 상태가 포함되면 공통 저장소 장애 시 모든 BE Target이 비정상으로 보일 수 있다. ECS 연결 전 BE 팀과 실제 Health 응답 및 준비 상태 계약을 확인하고, 필요하면 의존성을 제외한 전용 Readiness 경로로 변경한다.

현재 FE Image Workflow가 빌드 인자 `NEXT_PUBLIC_API_BASE_URL=https://yeodam-2gether.com/api`를 지정하고 Dockerfile이 이를 빌드에 전달한다. 따라서 같은 Image를 스테이징에 두기만 하면 브라우저 API 요청이 운영 BE로 향할 수 있다. 같은 Origin 방식을 쓰려면 **먼저 FE Image Workflow의 빌드 인자를 `/api`로 변경**해야 한다. Dockerfile 수정은 필수 사항이 아니다. 다만 현재 Next.js 코드는 같은 API 설정을 SSR 호출과 OAuth 이동에도 사용하므로, FE 팀이 이 두 경로를 별도로 처리해야 전체 흐름이 정상 동작한다. 이 변경과 실제 브라우저 검증 전에는 스테이징 사용자 흐름을 완료로 표시하지 않는다. 브라우저가 보는 `/api`와 BE가 받는 `/api`는 같은 요청 경로다. ALB가 그 경로로 목적지를 선택한다.

## TLS, 공개 DNS와 네트워크 경계

ALB는 두 Public Subnet에 배치하고 80 요청을 443으로 301 Redirect한다. `staging.yeodam-2gether.com` ACM 인증서는 서울 리전에서 발급하고 기존 `yeodam-2gether.com` Public Hosted Zone에 **인증서 검증 CNAME**을 만든다. HTTPS Listener는 인증서 발급 검증 후 생성된다. 기존 운영 Apex A Record는 변경하지 않는다.

이번 Terraform에는 `staging.yeodam-2gether.com`의 **서비스 Alias A Record가 없다.** FE와 BE ECS Service가 Target Group에 등록되고 Healthy 상태가 된 뒤 별도 작업에서 ALB DNS/Hosted Zone ID를 사용해 Alias를 연결한다. 그 전에는 `staging` 주소로 실제 서비스를 사용할 수 없다. Route 53의 인증서 CNAME 생성은 운영 서비스 Record 변경과 다르지만, 같은 Public Hosted Zone에 쓰기를 수행하므로 적용 전 Hosted Zone ID를 다시 확인한다.

ALB Security Group은 인터넷의 TCP 80/443만 받고, Private App Subnet `10.30.10.0/24`의 FE 3000/BE 8080으로만 보낸다. 다음 ECS 작업에서 Task Security Group의 해당 포트 Ingress를 **ALB Security Group 출처로 제한**해야 한다. 현재 ALB만 구성한 상태로는 Target이 없다. Target Group은 `ip` 유형이며 Fargate `awsvpc` 연결용이다. 두 Target Group은 15초 간격, 5초 Timeout, 연속 2회 성공/실패, 60초 Deregistration Delay로 설정했다. 이 수치는 구성값이며 장애 감지 시간과 실제 요청 실패 범위는 다중 Task 시험에서 측정한다.

## 비용과 적용 시점

2026-10-07 AWS Pricing API의 서울 리전 ALB 단가는 시간당 **$0.0225**, LCU는 시간당 **$0.008/LCU**다. 730시간 상시 운영 시 ALB 시간 요금은 **$16.43/월**이다. ALB의 공인 IPv4 2개에 각각 $0.005/시간이 청구된다고 가정하면 **$7.30/월**이 더해져 약 **$23.73/월 + LCU/전송 요금**이다. 이 공인 IPv4 수량은 적용 후 AWS 청구 항목에서 확인한다. [AWS ALB 요금](https://aws.amazon.com/elasticloadbalancing/pricing/), [공인 IPv4 요금](https://aws.amazon.com/vpc/pricing/)

앞선 [스테이징 네트워크](v2-staging-network.md)의 NAT 및 EIP 약 $46.72/월과 합치면, 위 가정의 **네트워크와 ALB 상시 기본료만 약 $70.45/월**이다. ECS Task, DB, Redis, LCU, NAT 처리량과 데이터 전송은 제외한 값이다. 코드 병합만으로 AWS 비용은 생기지 않으며, #124 네트워크와 이 ALB를 실제로 적용할 시점은 ECS/상태 저장소 준비와 묶어서 정한다.

## Terraform 검증과 이후 확인

명령은 Cloud 저장소 루트 `/Users/lee-y.ch/Desktop/yeodam/KTB4-2nd-Cloud`에서 실행한다. `backend.hcl`에는 [네트워크 문서](v2-staging-network.md)의 실제 state bucket을 설정하고 Git에 포함하지 않는다.

```bash
export AWS_PROFILE=yeodam-admin
aws sts get-caller-identity
aws route53 get-hosted-zone --id Z09268031LIP9TICUQ911
terraform fmt -check -recursive terraform/v2
terraform -chdir=terraform/v2/staging init -backend-config=backend.hcl
terraform -chdir=terraform/v2/staging validate
terraform -chdir=terraform/v2/staging plan -input=false
```

계정은 여담 AWS 계정, 리전은 `ap-northeast-2`, Hosted Zone은 `yeodam-2gether.com`인지 확인한다. #126 시점의 미적용 State Plan은 **30개 생성, 변경 0, 삭제 0**이었다. 이는 #124 네트워크 15개와 #126 ALB/TLS/DNS 검증 15개다. 이후 #128 [ECS 실행 기반](v2-staging-ecs-foundation.md) 6개와 #131 [사진 저장소](v2-staging-photo-storage.md) 10개가 추가돼 Plan은 **46개 생성, 변경 0, 삭제 0**이 됐다. 기존 V1 자원 변경·삭제와 스테이징 서비스 Alias 생성은 없었다. Plan은 변경되기 쉬우므로 적용 직전 다시 생성하고 각 자원과 비용을 검토한다. 저장한 Plan 파일과 State는 Git에 넣지 않는다.

적용 뒤에는 ACM 상태가 `ISSUED`인지, HTTP가 HTTPS로 Redirect되는지, 두 Target Group의 Healthy 수가 실제 Task 수와 맞는지 순서대로 확인한다. Alias 연결 전에는 `staging` 도메인의 브라우저 요청 성공을 기대하지 않는다. Alias를 연결한 뒤에는 화면, `/api/health`, 실제 BE API를 각각 호출해 응답 출처와 Target Group을 확인한다. FE/BE Task 하나를 종료하거나 Rolling 배포하며 실패 요청, Target 제외 및 복귀 시간은 후속 다중 인스턴스 시험에서 기록한다. ACM이 `PENDING_VALIDATION`에 머무르면 CNAME의 Zone과 실제 공개 DNS 응답을 확인한다. Target이 Unhealthy이면 앱 경로와 Status Code, Task Security Group, 컨테이너 포트, 앱 기동 로그를 차례로 확인한다.
