# V2 스테이징 네트워크와 Terraform 경계

V2 스테이징은 디버깅과 다중 Task 부하·장애 시험에 사용한다. V1 운영 VPC의 NAT, 라우팅, 데이터 저장소를 공유하면 시험 부하와 장애 주입의 영향 범위를 구분하기 어렵다. 따라서 **같은 AWS 계정에 별도 VPC**를 만드는 방향으로 Terraform을 작성했다. 운영 V2도 이후 별도 환경으로 구성하며, 이 문서의 스테이징 자원을 그대로 운영 자원으로 승격하지 않는다.

2026-10-07 현재 이 문서는 **Terraform 코드와 Plan 검증 결과**다. AWS에 스테이징 자원을 적용하거나 FE/BE Task의 실제 통신을 확인한 결과가 아니다.

## 이번 작업의 경계

```text
Internet
  │
  ├─ Public Subnet A (ap-northeast-2a, 10.30.0.0/24)
  │    └─ NAT Gateway + Elastic IP
  └─ Public Subnet B (ap-northeast-2c, 10.30.1.0/24)
       └─ 두 Public Subnet은 다음 작업의 ALB가 사용

Private App Subnet A (ap-northeast-2a, 10.30.10.0/24)
  ├─ 0.0.0.0/0 → NAT Gateway → 외부 OAuth, GHCR 등
  └─ 서울 리전 S3 → Gateway Endpoint
```

VPC는 `10.30.0.0/16`이며 DNS 지원과 Hostname을 켠다. Private Subnet에는 Public IP를 자동 할당하지 않는다. ALB가 요구하는 두 AZ의 Public Subnet을 준비하되 **ALB, ECS, MySQL, Redis, Queue와 Security Group은 아직 만들지 않는다.** App·DB·Redis를 가용 영역 A에 두는 것은 4단계 설계의 Single-AZ 출발점이며, Task 하나의 교체와 AZ 전체 장애를 같은 가용성으로 주장하지 않는다. 초기 알림은 이후 구현될 짧은 Polling 요청 기준이다.

실제 계정의 기존 VPC CIDR은 V1 `10.20.0.0/16`과 기본 VPC `172.31.0.0/16`으로, 제안한 `10.30.0.0/16`과 겹치지 않았다. `ap-northeast-2a`와 `ap-northeast-2c`도 2026-10-07 계정에서 사용 가능한 것으로 조회했다. 적용 직전에 다시 확인한다.

### 왜 V1 VPC 안에 추가하지 않는가

같은 VPC에 전용 Subnet과 Security Group을 두는 것도 가능하다. 그러나 스테이징이 운영 NAT와 라우팅을 공유하면 부하·장애 시험 때 외부 통신 비용과 실패 원인을 분리하기 어렵고, 잘못된 연결이 운영 데이터에 닿을 위험도 커진다. 별도 VPC에는 NAT Gateway와 공인 IPv4의 상시 비용이 생긴다. 이 비용을 아래에 명시하고, 스테이징 시험의 격리와 재현성을 선택했다. 계정 자체는 공유하므로 IAM, 서비스 Quota와 청구의 완전한 분리까지 얻는 것은 아니다.

## Terraform state와 재사용 범위

기존 `terraform/` 루트는 V1 전용이며 state key가 `yeodam/v1/terraform.tfstate`로 고정돼 있다. V2 스테이징은 `terraform/v2/staging/`의 별도 root와 `yeodam/v2/staging/terraform.tfstate`를 사용한다. 같은 기존 S3 state bucket을 쓰지만 객체 key와 Lock은 분리된다. 따라서 V1 root를 V2 변수로 덮어쓰거나 기존 V1 자원을 import/move하지 않는다. 같은 bucket이므로 state 접근 권한까지 환경별로 분리한 것은 아니며, 권한 경계는 배포 역할 작업에서 다시 다룬다.

네트워크 리소스는 `terraform/v2/modules/network/`에 두어 나중에 V2 운영 환경에서도 같은 구조를 재사용할 수 있게 했다. 환경별 CIDR, 이름, AZ와 state는 각 root가 소유한다. 모듈을 공유하더라도 운영과 스테이징이 하나의 VPC나 state를 공유하지는 않는다.

## 예상 네트워크 비용

2026-10-07 AWS Pricing API의 서울 리전 On-Demand 단가 기준이다. 아래 월액은 **730시간 연속 유지 가정**이며 아직 실제 청구액이 아니다.

| 항목 | 단가 | 730시간 가정 |
| --- | ---: | ---: |
| NAT Gateway 1개 | $0.059/시간 | $43.07 |
| NAT용 공인 IPv4 1개 | $0.005/시간 | $3.65 |
| 위 두 항목의 상시 합계 | | **$46.72/월** |

NAT 데이터 처리는 별도 **$0.059/GB**이며 일반 데이터 전송 요금이 추가될 수 있다. S3 Gateway Endpoint에는 별도 시간 요금이 없어 Private App Subnet의 서울 리전 S3 트래픽을 NAT에서 뺀다. ALB, FE/BE Task, MySQL, Redis, Queue와 GPU 비용은 이번 네트워크 계산에 들어 있지 않다. NAT는 Task를 0개로 줄여도 계속 과금되므로, 스테이징을 장기간 사용하지 않을 때는 전체 환경의 보존·재생성 방식을 별도로 결정해야 한다. [AWS VPC 요금](https://aws.amazon.com/vpc/pricing/), [NAT 과금 방식](https://docs.aws.amazon.com/vpc/latest/userguide/nat-gateway-pricing.html), [S3 Gateway Endpoint](https://docs.aws.amazon.com/vpc/latest/privatelink/vpc-endpoints-s3.html)

## 검증과 적용 절차

명령은 **Cloud 저장소 루트**인 `/Users/lee-y.ch/Desktop/yeodam/KTB4-2nd-Cloud`에서 실행한다. 로그인한 프로필은 여담 AWS 계정의 서울 리전을 가리켜야 한다. 예시의 `<STATE_BUCKET>`에는 이미 V1 Terraform이 사용하는 실제 S3 state bucket 이름을 넣는다. `backend.hcl`은 Git에서 제외된다.

```bash
export AWS_PROFILE=yeodam-admin
aws sts get-caller-identity
aws ec2 describe-vpcs --region ap-northeast-2 --query 'Vpcs[].[VpcId,CidrBlock]' --output table

cp terraform/v2/staging/backend.hcl.example terraform/v2/staging/backend.hcl
# backend.hcl의 bucket을 실제 <STATE_BUCKET>으로 바꾼다.
terraform fmt -check -recursive terraform/v2
terraform -chdir=terraform/v2/staging init -backend-config=backend.hcl
terraform -chdir=terraform/v2/staging validate
terraform -chdir=terraform/v2/staging plan -input=false
```

`init`은 V2 스테이징 state key를 표시해야 한다. `plan`에서 현재 첫 적용의 예상은 **15개 생성, 변경 0, 삭제 0**이며 V1 자원의 주소나 ID가 나오면 중단한다. 적용 전에는 계정/리전, CIDR 중복, NAT 상시 비용과 생성·변경·삭제 수를 다시 확인한다. 이 문서 작성 시점에는 적용하지 않았으므로 VPC ID와 실제 라우팅 결과는 없다.

적용 후에는 `terraform output`의 VPC/Subnet/NAT/S3 Endpoint ID를 확인하고, AWS의 Route Table에서 Public `0.0.0.0/0 → IGW`, Private `0.0.0.0/0 → NAT`, S3 Prefix List `→ Gateway Endpoint`를 대조한다. 이어 `terraform plan`이 변경 0건인지 확인한다. 실제 외부 API·GHCR/S3 통신과 ALB 도달성은 다음 FE/BE Task 작업에서 검증한다.

AWS CLI가 `Your session has expired`를 반환하면 `aws login --profile yeodam-admin`으로 해당 프로필을 갱신하고 `aws sts get-caller-identity --profile yeodam-admin`을 확인한다. 기본 프로필에 로그인했더라도 명령이 사용하는 프로필이 다르면 같은 오류가 날 수 있다. `terraform validate`에서 Provider Plugin 실행 오류가 나면 Provider 설치 경로와 실행 환경을 확인하고 다시 실행한다. `plan`에서 기존 자원 변경·삭제가 나타나면 적용하지 말고 현재 `-chdir` 경로, backend key와 사용 프로필을 먼저 점검한다.
