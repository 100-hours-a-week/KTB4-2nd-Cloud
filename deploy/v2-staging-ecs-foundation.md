# V2 스테이징 ECS 실행 기반

2026-10-07 Issue #128은 [스테이징 네트워크](v2-staging-network.md)와 [ALB/HTTPS](v2-staging-alb.md) 다음에 FE/BE Fargate Task가 사용할 **공통 실행 기반**을 Terraform에 추가한다. 작성 시점에는 코드와 실제 AWS 계정 Plan만 검증했다. AWS `apply`, Task 시작, Image Pull, 로그 수집을 실제 확인한 결과가 아니다.

## 이번 작업의 실행 경계

`terraform/v2/modules/ecs-foundation/`이 다음 여섯 자원을 만든다.

| 자원 | 역할 |
| --- | --- |
| ECS Cluster | 스테이징 FE/BE Service가 연결될 실행 단위 |
| FE/BE CloudWatch Log Group | 환경별 Task 로그 분리, 30일 보존 |
| GHCR Credentials Secret | 비공개 Image Pull에 사용할 인증정보의 저장 위치만 생성 |
| Task Execution Role·Policy | ECS가 위 Secret을 읽고 두 Log Group에 기록 |

Secrets Manager **Secret 값과 Version은 Terraform에 넣지 않는다.** 실제 GHCR 계정명·읽기 권한 Token은 Task 실행 전에 권한 있는 운영자가 AWS Secrets Manager에 `username`과 `password` 키를 가진 JSON 값으로 입력한다. CLI 인자, Git, `.env`, Terraform 변수·State·Plan 파일에 Token을 남기지 않는다. Secret 값이 비어 있는 상태에서 Task Definition에 `repositoryCredentials`를 연결하면 Image Pull이 실패한다. Secret 이름은 `yeodam/v2/staging/ghcr-credentials`이며, 운영 환경에서는 별도 이름과 권한을 사용해야 한다.

Execution Role은 `ecs-tasks.amazonaws.com`만 Assume할 수 있다. 부여한 권한은 두 Log Group의 `CreateLogStream`·`PutLogEvents`와 해당 GHCR Secret의 `GetSecretValue`뿐이다. **애플리케이션이 S3·DB·Queue에 접근할 Task Role과는 별개다.** FE와 BE Task Role은 서비스별 필요 권한을 확인한 뒤 후속 Issue에서 분리한다. Task Definition의 `executionRoleArn`에 이번 Role을 넣고, `taskRoleArn`에는 각 서비스 Role을 넣어야 한다. GHCR은 승인된 V2 설계대로 유지하며, Fargate의 Private Subnet에서 NAT를 통해 Registry에 연결한다.

이번 범위에는 Task Definition, ECS Service, Auto Scaling, Task Security Group, ALB Target 등록, Container Insights와 Image 배포가 없다. Cluster와 Log Group이 생겨도 접속 가능한 스테이징 앱은 아직 없다. 이후 MySQL·Redis, FE/BE Service와 Secret·Task Role 연결이 필요하다. 첫 Task 기동 시에는 NAT 경로, GHCR 인증정보, Image Digest, `awslogs` 설정, 컨테이너 Health 응답을 각각 확인한다.

## 적용 전 검증과 비용

명령은 Cloud 저장소 루트 `/Users/lee-y.ch/Desktop/yeodam/KTB4-2nd-Cloud`에서 실행한다. `backend.hcl`은 [네트워크 문서](v2-staging-network.md)에 따라 로컬에만 작성한다. 예시는 실제 여담 AWS 계정의 스테이징 State bucket을 사용한다.

```bash
export AWS_PROFILE=yeodam-admin
aws sts get-caller-identity
terraform fmt -check -recursive terraform/v2
terraform -chdir=terraform/v2/staging init -backend-config=backend.hcl
terraform -chdir=terraform/v2/staging validate
terraform -chdir=terraform/v2/staging plan -input=false
```

2026-10-07 검증 대상은 AWS 계정 `483175530259`, 서울 리전 `ap-northeast-2`다. 기존 미적용 스테이징 네트워크 15개와 ALB 관련 15개, 이번 ECS 기반 6개를 합한 Plan은 **36개 생성, 변경 0, 삭제 0**이었다. 기존 V1 자원 주소와 공개 스테이징 서비스 Alias는 없다. 실제 적용 직전에는 이 Plan을 다시 생성하고 대상 계정·리전·자원 이름·생성/변경/삭제 수를 확인한다. 저장한 Plan에는 민감한 값이 포함될 수 있으므로 Git에 넣지 않는다.

ECS Cluster와 IAM Role 자체의 상시 실행 요금은 없지만, Secret은 저장 기간과 API 호출, CloudWatch Logs는 수집·보존·조회량에 따라 과금된다. AWS Secrets Manager 공개 요금 예시의 Secret 1개 비용은 **$0.40/월**이며 호출 요금은 별도다. 이번 Log Group은 Task가 없어 아직 앱 로그 수집량을 예측할 수 없다. 이후 FE/BE Task를 띄울 때 Fargate vCPU·Memory, NAT 데이터 처리와 로그 수집량을 함께 측정한다. [Secrets Manager 요금](https://aws.amazon.com/secrets-manager/pricing/), [CloudWatch 요금](https://aws.amazon.com/cloudwatch/pricing/)

## 적용 후와 첫 Task 실행 때 확인할 것

적용 후 `terraform output`에서 ECS Cluster ARN, Execution Role ARN, GHCR Secret ARN, FE/BE Log Group 이름을 확인한다. AWS Console의 ECS Cluster에는 **Service와 Task가 0개**여야 하고, Secrets Manager에는 이름만 생성된 Secret이 보여야 한다. 실제 자격증명을 넣은 뒤에는 값을 출력하는 조회 명령 대신 Secret Version 존재 여부만 확인한다. Task Definition에서는 Secret ARN을 `repositoryCredentials.credentialsParameter`로 참조하고, Execution Role ARN과 서비스별 Log Group 이름을 사용한다.

Image Pull 실패 시 `ResourceInitializationError`의 원인을 먼저 나눈다. `AccessDenied`면 Execution Role과 Secret ARN을, 인증 실패면 GHCR Token의 Package 읽기 권한과 만료를, 연결 Timeout이면 NAT와 DNS·라우팅을 확인한다. 로그가 없다면 Task가 Image Pull 전에 실패했는지, Task Definition의 `awslogs` Region/Group, Execution Role의 로그 권한을 확인한다. FE/BE 앱 Health와 ALB Target 상태는 Service가 추가된 뒤 검증한다.
