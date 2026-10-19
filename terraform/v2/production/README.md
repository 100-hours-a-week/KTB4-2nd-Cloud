# 운영 V2 사전 배포 기반

실행 위치는 Cloud 저장소의 `terraform/v2/production`이다. 기존 운영 VPC에 V2 실행 기반을 추가한다. 현재 서비스 요청은 V1 Nginx가 계속 받는다.

| 구분 | 이번 작업 |
| --- | --- |
| AWS | Account `483175530259`, 서울 `ap-northeast-2` 고정 |
| 기존 자원 | V1 VPC `10.20.0.0/16`, Public `10.20.1.0/24`, IGW는 data source로만 참조 |
| 추가 네트워크 | Public `10.20.2.0/24`(2c), Private `10.20.10.0/24`(2a), 단일 NAT, 새 Route Table과 S3 Gateway Endpoint |
| ALB | 기존 Public/새 Public의 2개 AZ, HTTP→HTTPS, FE/BE 빈 Target Group, Access Log 전용 S3 |
| TLS | `yeodam-2gether.com` ACM 인증서, 검증용 CNAME만 생성 |
| ECS | Cluster, FE/BE Log Group, GHCR Secret 메타데이터, 제한된 Task Execution Role |
| State | 기존 State 버킷의 `yeodam/v2/production/terraform.tfstate` |

기존 V1 자원은 루트 `terraform/` State가 소유한다. 이 디렉터리로 import하거나 루트에서 V1 자원을 제거하지 않는다. 새 Private Route Table에는 별도 S3 Endpoint를 연결하므로 V1 Endpoint의 Association도 바꾸지 않는다. V1 Public에 NAT ENI를 추가하지만 해당 서브넷의 기존 기본 경로는 그대로 IGW다.

## 초기화와 Plan

Cloud 저장소 루트에서 실행한다. SSO는 로컬 관리 계정이며 Terraform에 장기 Access Key를 저장하지 않는다.

```bash
export AWS_PROFILE=yeodam-admin
export AWS_REGION=ap-northeast-2
aws sts get-caller-identity
cp terraform/v2/production/backend.hcl.example terraform/v2/production/backend.hcl
chmod 600 terraform/v2/production/backend.hcl
terraform -chdir=terraform/v2/production init -input=false -backend-config=backend.hcl
terraform -chdir=terraform/v2/production fmt -check
terraform -chdir=terraform/v2/production validate
```

계정이 다르거나 기본 VPC/Subnet 조건이 다르면 진행하지 않는다. `No valid credential sources`는 프로필 누락과 SSO 만료를 구분한다. `AWS_PROFILE`을 확인하고 같은 프로필의 STS 조회도 실패할 때 `aws sso login --profile yeodam-admin`을 수행한다.

Plan은 비공개 파일로 보존한다. 같은 이름의 기존 Plan을 재사용하지 않는다.

```bash
umask 077
mkdir -p /private/tmp/yeodam-production-foundation
terraform -chdir=terraform/v2/production plan -input=false \
  -out=/private/tmp/yeodam-production-foundation/foundation.tfplan
terraform -chdir=terraform/v2/production show -json \
  /private/tmp/yeodam-production-foundation/foundation.tfplan \
  > /private/tmp/yeodam-production-foundation/foundation-plan.json
```

최초 Plan은 신규 자원만 생성하고 변경 0·삭제 0이어야 한다. V1/스테이징 EC2, DB, Route, 기존 DNS 레코드, Secret 값, ECS Service 변경이 있으면 Apply하지 않는다.

기존 `yeodam-2gether.com` A/AAAA와 관련 Alias/TTL을 보존하고, Plan의 Route53 변경은 새 ACM 검증 CNAME뿐인지 확인한다. 최초 적용 이후에는 실제 관리 State와 차이를 확인해 판단한다.

## 검토한 Plan 적용

위 Account/Region/생성·변경·삭제 수와 기존 자원 보존을 확인한 **바로 그 Plan**만 적용한다.

```bash
terraform -chdir=terraform/v2/production apply -input=false \
  /private/tmp/yeodam-production-foundation/foundation.tfplan
terraform -chdir=terraform/v2/production output
terraform -chdir=terraform/v2/production plan -input=false
```

NAT와 ALB는 생성 후 비용이 발생하며, 이 작업에는 GPU/DB/Redis/Kafka EC2를 생성하는 코드가 없다. 마지막 Plan은 `No changes`가 기대값이다. 중간 실패가 나면 State와 실제 생성 자원을 읽고 원인을 해결한 뒤 새 Plan을 검토한다. 일괄 Destroy나 V1 State 이동으로 복구하지 않는다.

## 적용 후 확인

- ALB `active`, NAT `available`, ECS Cluster `ACTIVE`인지 확인한다.
- HTTPS Listener 인증서가 ISSUED이고 `yeodam-2gether.com`을 포함하는지 확인한다.
- FE/BE Target Group의 등록 대상은 0개, Cluster의 Service/Task도 0개여야 한다.
- 운영 도메인의 A/AAAA/Alias와 V1 EC2·기존 Route/Endpoint Association이 적용 전과 같은지 확인한다.
- HTTPS를 시험할 때 DNS를 변경하지 않고 curl `--connect-to`로 **현재 PC의 해당 요청만** 새 ALB에 연결한다. `-k`로 인증서 검증을 끄지 않는다.

```bash
ALB_DNS="$(terraform -chdir=terraform/v2/production output -raw alb_dns_name)"
curl --connect-to "yeodam-2gether.com:443:${ALB_DNS}:443" \
  --connect-timeout 5 --max-time 15 -sS -o /dev/null -w '%{http_code}\n' \
  https://yeodam-2gether.com/api/actuator/health
```

앱이 아직 없으므로 이때의 예상 응답은 ALB의 **503**이다. 이는 TLS/라우팅 기반의 확인이며 앱 Health 통과가 아니다. 공개 운영 도메인으로 일반 접속한 사용자는 기존 V1을 계속 이용한다.

ACM 검증이 지연되면 생성된 검증 CNAME과 Public Hosted Zone을 확인한다. ALB 로그 오류는 새 로그 버킷 정책·서울 Region·SSE-S3 설정을 확인한다. 이 로그 버킷은 사용자 사진 저장소가 아니다.

## 앱 기동까지 남은 조건

GHCR Secret은 비어 있으며 자격증명을 Terraform 코드/State에 넣지 않는다. 운영용 DB·Redis·Kafka·AI와 Task Role/환경변수/Secrets/Service/CD는 후속 작업이다. 사전 배포 앱을 활성 V1 DB에 바로 연결하면 Flyway·Consumer·Scheduler가 사용자 접속 없이 쓸 수 있으므로 격리 상태에서 기동부터 확인한다. Flyway만 끄는 것을 전체 부수 효과 차단으로 간주하지 않는다.

운영 DNS 변경, 업무 DB 이전, SQL 인증→Redis 이행, 기존 업로드/AI Drain과 V1 종료는 이 기반 적용에 포함되지 않는다. 스테이징 리허설 결과와 각 선행 조건을 확인한 뒤 실행한다. 기존 스테이징 자동 CD가 운영을 대상으로 삼도록 변경하지 않는다.
