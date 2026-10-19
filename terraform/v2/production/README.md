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
| MySQL | 별도 Private `t4g.medium`, Root 30GiB·데이터 gp3 120GiB, MySQL9.7.2, 새 빈 `yeodam` DB |
| 인증 Redis | 별도 ElastiCache `cache.t4g.micro`, Redis7.1 Single-AZ, TLS/AUTH·저장 암호화·noeviction |
| Kafka | 별도 Private `t3.medium`, 데이터 gp3 100GiB, 단일 KRaft Broker와 Connect, 운영 Topic 분리 |
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

NAT·ALB·MySQL·Redis·EBS는 생성 후 비용이 발생한다. GPU는 이 구성에 없다. 마지막 Plan은 `No changes`가 기대값이다. 중간 실패가 나면 State와 실제 생성 자원을 읽고 원인을 해결한 뒤 새 Plan을 검토한다. 일괄 Destroy나 V1 State 이동으로 복구하지 않는다.

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

GHCR Secret은 비어 있으며 자격증명을 Terraform 코드/State에 넣지 않는다. MySQL·인증 Redis는 아래 절차로 준비하며 Kafka 실행은 아래 #329 절차로 준비하며 AI와 앱 Task Role/환경변수/Secrets/Service/CD는 후속 작업이다. 사전 배포 앱을 활성 V1 DB에 바로 연결하면 Flyway·Consumer·Scheduler가 사용자 접속 없이 쓸 수 있으므로 새 빈 DB에서 기동부터 확인한다. Flyway만 끄는 것을 전체 부수 효과 차단으로 간주하지 않는다.

운영 DNS 변경, 업무 DB 이전, SQL 인증→Redis 이행, 기존 업로드/AI Drain과 V1 종료는 이 기반 적용에 포함되지 않는다. 스테이징 리허설 결과와 각 선행 조건을 확인한 뒤 실행한다. 기존 스테이징 자동 CD가 운영을 대상으로 삼도록 변경하지 않는다.

## #327 MySQL·인증 Redis 준비

신규 MySQL은 현재 운영 V1과 같은9.7.2 Digest를 사용하며, 사양은 승인된 V2 기준이다. 기존 운영의 `yeodam` DB와 새 EC2의 빈 `yeodam` DB는 **이름만 같고 다른 서버**다. 새 호스트 `mysql-predeploy.production.yeodam.internal`만 제공하고 활성 Writer DNS나 V1→V2 복제 채널은 만들지 않는다.

Root Secret은 `yeodam-v2-production/mysql-root-password`, 앱 비밀번호는 `/yeodam/v2/production/app/MYSQL_PASSWORD`다. 값은 Terraform 밖에서 생성한다. Redis AUTH는 모듈의 ephemeral 값/쓰기 전용 속성으로 저장해 평문을 State에 남기지 않는다.

```bash
# Cloud 저장소 루트. 누락된 운영 DB 자격증명만 생성하며 기존 값은 보존한다.
python3 scripts/provision-production-mysql-credentials.py

# cloud-init 완료·MySQL SELECT 1 확인 뒤, 새 빈 호스트에서만 최초 계정을 생성한다.
mysql_instance=$(terraform -chdir=terraform/v2/production output -raw mysql_instance_id)
aws ssm send-command --profile yeodam-admin --region ap-northeast-2 \
  --instance-ids "$mysql_instance" \
  --document-name yeodam-v2-production-mysql-app-account \
  --query Command.CommandId --output text
```

반환된 Command ID를 같은 Instance ID의 `aws ssm get-command-invocation`으로 조회한다. `Success`와 계정 생성 완료 문구가 기대값이다. SSM 문서는 Bash로 실행하며 IMDSv2의 Instance ID가 계획한 새 호스트인지 확인한다. 새 DB가 비어 있지 않거나 복제가 설정됐으면 비밀번호/권한 변경을 거절한다. 이미 앱이나 이전 데이터가 들어간 뒤에는 이 최초 생성 절차를 다시 실행하지 않는다.

`yeodam_app`은 `10.20.10.%` 출발지에서 `yeodam.*`만 접근하며, 전역 권한이나 GRANT OPTION은 없다. 해당 스키마의 DDL은 Flyway에 필요해 허용하지만 글로벌 관리 권한과 구분한다. 네트워크는 BE SG→MySQL3306/Redis6379만 허용하고 공인 IP·SSH는 없다. Redis는 TLS와 AUTH를 함께 사용한다. 검증용 `files/verify-storage.py`는 향후 BE SG를 붙인 일회성 Fargate Task에서만 새 저장소에 임시 테이블/키를 쓰고 지운다. 이 Task는 앱 Service가 아니며 종료 뒤 등록한 검증 Task Definition을 해제한다.

MySQL 데이터 EBS는 별도 보존하며, 새 백업 버킷은 Versioning·SSE-S3·공개 차단·14일 보존을 사용한다. 전체 dump는 목요일03:00 KST, 닫힌 Binary Log 보존은 매분 수행한다. 백업 확인은 새 호스트의 `yeodam-mysql-full-backup.service`와 완료 `.ready` 오브젝트를 확인한다. 업로드 성공을 실제 복원 검증으로 확대하지 않는다. 지표 Namespace는 `Yeodam/V2/Production/MySQL`이다.

본 데이터 이전 전에 사전 배포 앱을 정지하고, 시험 데이터 보존/정리와 최종 스키마·Writer·백업 범위를 확정한다. 기존 운영 데이터를 새 빈 DB에 덮어쓰거나 사용자를 새 DB로 연결하는 동작은 이 절차에 없다. GPU는 기존 한 대를 운영/스테이징이 공유할 예정이나 네트워크·Qdrant·실행기 구분 방식은 별도 결정하며, 이 Terraform에는 GPU 변경이 없다.


## #329 Kafka·Connect 실행

새 Kafka EC2(t3.medium)는 Private2a에 두고 데이터는 암호화 gp3 100GiB에 저장한다. 스테이징과 같은 Kafka4.1.2·Debezium Connect3.4.3.Final Digest를 사용하되 DNS, KRaft ID, Topic과 Connect Group은 운영 전용이다. EC2 종료 방지·데이터 EBS prevent_destroy는 실수에 대한 보호이며 단일 Broker/RF1의 장애 복구나 고가용성을 보장하지 않는다. 데이터 EBS는 별도이고 루트20GiB는 종료 시 삭제된다.

`kafka-bootstrap.sh.tftpl`은 Docker와 EBS 마운트만 준비한다. 파일시스템이 예상과 다르면 포맷하지 않고 중단한다. cloud-init 완료 후 다음을 Cloud 저장소 루트에서 실행한다. 계정은 `yeodam-admin`, Region은 서울이다.

```bash
kafka_instance=$(terraform -chdir=terraform/v2/production output -raw kafka_instance_id)
# 먼저 SSM에서 mountpoint /data/kafka와 아래 파일 존재를 확인한다.
# /var/lib/yeodam-kafka-host-ready
aws ssm send-command --profile yeodam-admin --region ap-northeast-2 \
  --instance-ids "$kafka_instance" \
  --document-name yeodam-v2-production-kafka-runtime \
  --query Command.CommandId --output text
```

반환 Command ID와 Instance ID를 `aws ssm get-command-invocation`에 넣어 `Success`와 `Kafka and Connect are ready`를 확인한다. 실패하면 같은 서버의 cloud-init/마운트와 Compose 로그를 먼저 확인한다. 설치 도구는 IMDSv2의 실제 Instance ID를 계획한 새 EC2 ID와 비교하고, 준비 마커·EBS 마운트가 없으면 쓰기 전에 중단한다. 기존 데이터가 있는데 `.cluster-id`가 없거나 `meta.properties`의 ID와 다르면 자동 재포맷하지 않는다.

실행 파일은 `deploy/v2-production/kafka/`의 Compose·Topic 스크립트에서 SSM 문서로 전달한다. 설치된 Compose는 `/opt/yeodam-kafka/compose.yml`, Cluster ID 환경 파일은 `/etc/yeodam/kafka/runtime.env`다. `yeodam-kafka.service`는 데이터 마운트에 의존하며 Docker가 준비된 뒤 시작한다. 재실행해도 Topic 삭제나 Consumer Offset 초기화는 수행하지 않는다.

| 구분 | 운영 설정 |
| --- | --- |
| Bootstrap | `kafka.production.yeodam.internal:9092` |
| 업무 Topic6개 | `prod.yeodam.{trip-create,trip-update,story-create}.{request,result}` |
| DLT6개 | 각 업무 Topic 뒤 `-dlt` |
| 업무/DLT | 각2 Partition·RF1, 각각7일/30일 보존 |
| 요청 Timestamp | BE 접수 처리 계약에 맞춘 `LogAppendTime` |
| Connect 내부3개 | `prod.connect-configs`1/`offsets`5/`status`1 Partition, compact·무기한 |
| Schema History1개 | `prod.yeodam.schema-history`,1 Partition·무기한 |

관리 대상은 총16개다. Broker가 만드는 내부 Topic은 별도다. AWS 환경 이름은 `production`이고 메시지 계약의 Topic Prefix는 `prod`다. Connect Group은 `connect-prod`이며 스테이징 Group/Topic을 공유하지 않는다.

9092는 운영 BE SG와 AI Kafka Client SG에서만 접근한다. Controller9093과 Connect REST8083은 호스트 loopback에만 바인딩하며 외부 ALB에 연결하지 않는다. 운영 관리자는 SSM으로 접속한다. Kafka Client 연결은 사설망 PLAINTEXT이며 TLS/SASL/ACL 구성은 아니다. 이미지 기본 Plugin 상위 경로 `/kafka/connect`에서 MySQL Connector를 찾으며 현재 쓰지 않는 Debezium scripting은 끈다. Broker의 주기적 컨테이너 Health는 JVM을 새로 띄우지 않는 TCP liveness다. 설치 시 Metadata 조회와 역할별 발행·소비 시험을 별도로 통과해야 준비 완료로 판정한다. Topic 조정은 기존 설정을 한 번 읽고 누락/차이가 있는 항목만 생성·보정한다.

기동 직후 `curl -fsS http://127.0.0.1:8083/connectors`의 기대값은 `[]`이다. #329에서는 DB 접속 계정이나 Outbox/Migration Connector를 등록하지 않는다. 사전 배포 DB와 실제 운영 데이터 이관 경계를 정한 뒤 별도 작업으로 연결한다. 기존 스테이징 Kafka·마이그레이션 Channel/Offset은 변경하지 않는다.

접속 검증 스크립트 `deploy/v2-production/kafka/verify-client.sh`는 운영 Private Subnet의 일회성 Fargate에서 사용한다. `KAFKA_CLIENT_ROLE=backend|ai`, `KAFKA_TEST_RUN_ID=<32자리 소문자 hex UUID>`를 주고 해당 역할 SG만 붙여 각각 실행한다. 공식 Kafka 이미지에서 Metadata→고유 시험 Topic 생성→1건 발행/소비→내용 비교→Topic 삭제를 확인한다. 시험 Topic 이름은 `prod.cloud.preflight.<ID>`이고 업무 Consumer Group을 읽거나 초기화하지 않는다. Task 종료와 Definition 해제까지 확인하며 이 결과를 실제 BE/AI 업무 처리나 부하 검증으로 해석하지 않는다.

AI Kafka Client SG는 운영 VPC 내 통신 경계만 준비했다. 기존 GPU의 VPC 연결·Qdrant 데이터 분리·정리 실행기 공유를 설정한 것은 아니다. GPU와 기존 운영 V1의 DB·인증·공개 DNS는 이 작업에서 변경하지 않는다.
