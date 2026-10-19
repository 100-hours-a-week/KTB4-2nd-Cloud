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
| AI Redis | 별도 Private `t4g.small`, Redis7.4.2, Root16GiB·데이터10GiB, AOF always·noeviction |
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


## #332 운영 AI Redis

AI 작업 상태와 내부 대기열은 인증 Redis와 별도 EC2에서 보관한다. 스테이징에서 검증한 Redis7.4.2 Digest·ARM64 Compose를 그대로 사용하고 URL/Parameter/데이터 볼륨/네트워크만 운영 전용으로 분리한다. 기존 공유 예정 GPU의 네트워크나 실행기는 이 작업에서 연결하지 않는다.

| 항목 | 운영 값 |
| --- | --- |
| 내부 주소 | `ai-redis.production.yeodam.internal:6379` |
| 인증 URL Parameter | `/yeodam/v2/production/ai/REDIS_URL` (SecureString) |
| EC2/EBS | t4g.small, Root16GiB·별도 암호화 gp3 데이터10GiB |
| Redis | maxmemory512MB, noeviction, appendonly yes, appendfsync always |
| 접속 | 운영 AI Client SG만6379 허용, 공인IP/SSH 없음, SSM 관리 |

생성한 EC2의 cloud-init이 Docker·EBS 마운트·Redis를 준비한다. Bootstrap은 EBS Serial을 확인하고 기존 파일시스템을 덮어쓰지 않는다. 비밀번호는 호스트에서 생성하고 Parameter가 있으면 값을 보존하며 overwrite하지 않는다. URL/비밀번호는 Terraform State와 명령 출력에 넣지 않는다. 기존 값의 Host·형식이 맞지 않거나 조회가 실패하면 재발급 대신 중단한다.

Compose는 `/opt/yeodam-ai-redis/compose.yml`, ACL·설정·비밀번호 파일은 `/etc/yeodam/ai-redis/`, 데이터는 `/data/redis/data`다. Docker 포트는 호스트 사설 IP에만 게시한다. `yeodam-ai-redis.service`는 데이터 마운트와 Docker에 의존한다. EC2 종료 방지와 데이터 EBS prevent_destroy를 사용하며, user_data 변경만으로 stateful EC2를 자동 교체하지 않는다. Bootstrap 파일 수정은 이미 실행된 호스트에 자동 반영되지 않으므로 기존 호스트의 동작을 확인한 뒤 별도 적용 절차를 정한다.

Cloud 저장소 루트에서 대상 ID만 조회한다. Parameter URL 값을 터미널에 출력하지 않는다.

```bash
ai_redis_instance=$(terraform -chdir=terraform/v2/production output -raw ai_redis_instance_id)
aws ssm start-session --profile yeodam-admin --region ap-northeast-2 \
  --target "$ai_redis_instance"
```

접속한 새 AI Redis EC2에서 다음을 확인한다.

```bash
sudo cloud-init status
sudo systemctl is-active yeodam-ai-redis
sudo mountpoint /data/redis
sudo docker inspect --format '{{.State.Health.Status}}' yeodam-ai-redis
```

기대값은 cloud-init done, active, mounted, healthy다. 기동 실패 시 `sudo tail -n 60 /var/log/cloud-init-output.log`와 해당 컨테이너 로그를 읽는다. 비밀번호/URL 파일의 내용을 출력하거나 DB/AOF 파일을 삭제해서 기동하지 않는다.

`files/verify-ai-redis.py`는 일회성 Fargate 검증용이다. 허용 모드에는 실행 역할이 Parameter를 `REDIS_URL` Secret으로 주입하고 `PROBE_RUN_ID`(32자리 hex)와 `EXPECTED_REDIS_IP`를 전달한다. 새 호스트에서 만든 같은 ID의 `cloud:preflight:332:<ID>` 임시 키를 컨테이너 재시작 후 조회하고, 미인증 거절·AUTH·읽기쓰기·시험 키 삭제를 확인한다. 비허용 모드는 인증값 없이 DNS의 실제 목적지를 확인한 다음 TCP Timeout만 차단 성공으로 판정한다. DNS 실패/Connection Refused를 보안 그룹 차단 성공으로 간주하지 않는다. 검증 역할과 Task Definition은 종료 후 정리한다.

Redis URL은 사설망의 AUTH 접속이며 TLS 구성이 아니다. AOF always와 데이터 EBS 보존은 단일 호스트의 데이터 기록 설정이다. 이번 확인은 새 컨테이너 재시작에 한정하며 EC2/EBS 장애나 별도 복구·고가용성 시험이 아니다. 실제 AI API/정리 실행기 연결과 공유 GPU 접근 경계는 후속 작업이다.


## #337 공유 GPU와 운영 Qdrant

GPU EC2는 기존 스테이징 VPC에 그대로 둔다. Production State는 기존 GPU·EBS를 가져와 관리하지 않고, 새 Peering/Route/보안 규칙/운영 DNS/운영 Qdrant SSM 문서만 소유한다. 기존 스테이징 이미지·Compose·컨테이너3개는 그대로 유지한다.

| 연결 | 주소/범위 |
| --- | --- |
| 운영 임베딩 | `http://ai-gpu.production.yeodam.internal:8000/v1` |
| 운영 Qdrant | `http://ai-gpu.production.yeodam.internal:6335` |
| 기존 스테이징 | 기존 `ai-gpu.staging.yeodam.internal`의8000/6333 유지 |
| 운영 Route | 운영 Private RT→기존 GPU IP/32→Peering |
| 반환 Route | 기존 GPU Private RT→운영 Private10.20.10.0/24→Peering |
| 접근 | 운영 AI SG만8000/6335, 기존 스테이징 AI SG는8000/6333 |

Peering은 같은 Account/Region의 두 VPC를 연결한다. 인터넷 공개 포트·V1 공개 Route·기본 NAT 경로·서브넷 Association은 바꾸지 않는다. 교차 환경 Qdrant 포트는 허용하지 않는다. 새 Route와 보안 규칙은 별도 Terraform 리소스여서 staging이 소유한 Route Table/SG 자체를 import하거나 재생성하지 않는다. 운영 Private Hosted Zone의 A Record가 기존 GPU 사설 IP를 가리키며 스테이징 DNS를 수정하지 않는다.

운영 Qdrant는 스테이징과 같은1.19.1 Digest를 사용한다. 기존 `/data/qdrant/storage`와 달리 `/data/qdrant/production/storage`를 마운트하며 별도 Compose Project/컨테이너/서비스로 실행한다. 컨테이너 내부6333을 호스트 사설IP6335에만 게시한다. EBS20GiB 자체는 공동 사용하며 새 볼륨이나 GPU를 만들지 않는다. 임베딩 모델은 기존 한 개를 공유한다.

Cloud 저장소 루트에서 다음을 실행한다. 새 SSM 문서는 정확한 GPU Instance ID·EBS Serial·마운트와 설치 마커를 확인한다. 다른 호스트/볼륨이나 기존 미확인 데이터가 있으면 중단하며 포맷·재귀 삭제·기존 스테이징 경로 chown은 하지 않는다.

```bash
gpu_instance=$(terraform -chdir=terraform/v2/production output -raw shared_gpu_instance_id)
aws ssm send-command --profile yeodam-admin --region ap-northeast-2 \
  --instance-ids "$gpu_instance" \
  --document-name yeodam-v2-production-qdrant-runtime \
  --query Command.CommandId --output text
```

Command ID로 `get-command-invocation`을 조회해 Success와 `Production Qdrant ready`를 확인한다. 준비 판정은 실제 `/readyz`200이며 실행 중인 PID만으로 판정하지 않는다. 실패하면 새 production 컨테이너와 `/opt/yeodam-production-qdrant/compose.yml`만 확인하고 기존 스테이징 Compose를 재배포하거나 GPU를 재부팅하지 않는다.

설치 위치는 `/opt/yeodam-production-qdrant`, 서비스는 `yeodam-production-qdrant.service`, 컨테이너는 `yeodam-ai-qdrant-production`이다. 기존 `yeodam-ai-gpu.service`와 별개다. `deploy/v2-production/qdrant/verify-client.py`는 각 환경의 AI SG를 붙인 일회성 Fargate에서 자기 환경의8000/Qdrant readiness와 반대 환경 Qdrant TCP Timeout을 확인한다. DNS 실패나 Connection Refused를 격리 성공으로 세지 않는다. 데이터 검증은 고유 시험 컬렉션에 같은 Point ID를 서로 다른 값으로 쓰고 조회·삭제하는 범위로 수행한다.

이 작업 뒤에는 기존 스테이징3개와 운영 Qdrant1개로 총4개 컨테이너다. 운영 정리 실행기는 운영 AI 배포 때 추가하며, 현재 스테이징 실행기의 Redis나 S3 설정은 바꾸지 않는다. 기존 GPU가 running인 상태를 그대로 유지한다. GPU의 배포·중지는 양쪽 환경에 영향을 주며, 컨테이너/데이터 경로 분리를 CPU·GPU·EBS 용량이나 장애 영역 분리로 해석하지 않는다. 이 작업은 기존 GPU 배포 워크플로우나 전원 정책을 변경하지 않는다.


## #343 운영 정리 실행기

기존 공유 GPU에 `yeodam-ai-process-runner-production`만 추가한다. 별도 Compose Project `yeodam-v2-production-ai-runner`, `/opt/yeodam-production-runner`, `/etc/yeodam/production-ai/runner.env`와 `yeodam-production-runner.service`를 사용한다. 실행은 `python -m app.jobs.runner`, CPU 상한2개이며 GPU Device Request는 없다. 이미지는 검증된 Cloud V2 Manifest의 ai-app Digest를 사용한다. 기존 staging runner나 모델/Qdrant 이미지를 갱신하는 도구가 아니다.

환경은 `JOBS_ENV=production`, 운영 AI Redis URL Parameter와 운영 Qdrant6335를 사용한다. 임베딩8000과 API Key는 기존 모델 한 개를 공유하므로 embedding credential은 기존 staging Parameter를 조회하되 수정하지 않는다. 운영 `API_KEY`는 없을 때만 호스트에서 생성하며 기존 값은 보존한다. 값을 Terraform State·SSM 명령 출력·로그에 쓰지 않는다. 운영 API 배포에서도 동일한 `/yeodam/v2/production/ai/API_KEY`를 조회해야 한다.

운영 사진은 기존 `yeodam-v1-app-data-483175530259-ap-northeast-2` 버킷을 유지한다. 버킷 자체는 V1 State 소유이며 새 버킷/정책으로 대체하지 않는다. 호스트 역할에는 `trip-uploads/*/derived/*`와 `trip-additions/*/derived/*`의 GetObject만 추가한다. 기존 사진 객체를 옮기거나 원본 읽기·쓰기 권한을 늘리지 않는다. 검증용 고유 derived 객체는 관리자 계정이 만들고 컨테이너에서 읽은 뒤 해당 Version ID만 삭제한다.

운영 Private Hosted Zone을 기존 GPU의 staging VPC에도 연결해 운영 Redis/GPU DNS를 조회한다. 이는 zone의 주소를 해석하도록 하는 연결이며 모든 운영 서비스의 네트워크 접근을 허용하는 변경은 아니다. Peering Route는 #337을 재사용하고 새 SG 규칙은 GPU↔운영 AI Redis6379 두 개뿐이다. process runner는 Redis에서 작업을 가져오므로 GPU의 운영 Kafka9092 접근을 이번에 추가하지 않는다. Topic 변수는 prod Prefix지만 실제 Kafka 소비·발행은 후속 AI API 역할이다.

설치 SSM은 `yeodam-v2-production-ai-runner-runtime`이다. 정확한 GPU/데이터 EBS·이미지 Digest·Redis 목적지를 검증하고, 미확인 컨테이너 이름 충돌은 거절한다. 임베딩·운영 Qdrant가 준비된 뒤 새 실행기만 기동하며 systemd의 기동 전에도 같은 준비 확인을 수행한다. systemd에서 기존 staging 서비스나 Docker를 재시작하지 않는다.

```bash
# Cloud 저장소 루트, 검토한 운영 Terraform 적용 후 실행
aws ssm send-command --profile yeodam-admin --region ap-northeast-2 \
  --instance-ids i-05e1ad9dd96dba4ae \
  --document-name yeodam-v2-production-ai-runner-runtime \
  --query Command.CommandId --output text
```

반환한 Command ID의 `get-command-invocation`이 Success이고 새 runner의 실제 Redis PING/모델 health/Qdrant readiness가 통과해야 한다. 실패하면 새 production 서비스/컨테이너와 해당 명령의 상태만 조사한다. 상태가 불명확하면 다른 설치를 겹쳐 실행하지 않는다. Env 파일·Docker 전체 inspect·SecretString을 출력하지 않는다. 운영 업무 작업을 테스트용으로 큐에 넣지 않는다.

공유 GPU는 staging CD 완료 후 자동 중지하지 않는다. 코드에서 stop 호출을 제거했고 기존 release 역할에도 해당 GPU의 StopInstances만 명시적Deny한다. StartInstances와 관리자 수동 운영 권한을 막는 정책은 아니다. GPU 비용은 실행 중 계속 발생하며 운영·staging이 모두 의존하므로 종료는 양쪽 작업 상태를 확인한 별도 운영 행위다.

동일 EC2/역할을 사용하는 실행기이므로 서로 다른 환경 설정·Redis/Qdrant 저장소를 사용하는 분리다. 컨테이너별 IAM 신뢰 경계나 CPU/GPU/EBS 장애 영역 분리로 주장하지 않는다. 공유 모델 변경·호스트 중지는 양 환경에 영향을 주며 동시 처리량·실제 여행 생성 E2E는 별도 검증이다. 이번 작업에서 운영 FE/BE/API나 공개 요청 경로를 배포·전환하지 않는다.

## #347 운영 AI API 사전 배포

운영 AI API는 기존 운영 ECS Cluster에 Fargate 0.5 vCPU/1GiB, 1 Task로 시작한다. Cloud Manifest의 AI Digest를 정리 실행기와 함께 사용한다. 기본 명령은 이미지의 `uvicorn app.v2_main:create_app --factory`다. 모델과 Qdrant를 이 컨테이너에 설치하지 않는다.

| 경로 | 설정 |
| --- | --- |
| BE → AI | `http://ai-api.production.yeodam.internal` → 내부 ALB80 → Task8000 |
| Task 위치 | 기존 운영 Private2a `10.20.10.0/24`, Public IP 없음 |
| ALB 위치 | Private2a + 새 Private2c `10.20.11.0/24` |
| Redis / Kafka | 운영 전용 주소, `JOBS_ENV=production`, Topic `prod.yeodam.*` |
| 임베딩 / Qdrant | 기존 공유 GPU8000/v1 / 운영 전용6335 |
| 로그 | `/yeodam/v2/production/ai-api`, 보존30일 |

ALB의80은 운영 BE SG만, Task의8000은 ALB SG만 허용한다. AI Task에는 기존 운영 AI Kafka Client SG를 붙여 Kafka9092/Redis6379/공유 GPU8000·6335/외부 HTTPS443만 사용한다. 두 번째 Private Subnet은 기존 Private RT를 재사용하며 새 NAT를 만들지 않는다. Task는 GPU 반환 Route가 있는2a에 유지하므로 ALB가2AZ라는 사실을 Task나 NAT의 고가용성으로 해석하지 않는다.

실행 역할은 지정한 Parameter5개·운영 GHCR Secret·자기 로그 그룹만 조회한다. Task 역할은 기존 운영 버킷의 `trip-uploads/*/derived/*`, `trip-additions/*/derived/*` GetObject만 가진다. 원본 읽기/객체 쓰기/DB 접속은 추가하지 않는다. 운영 API Key와 Redis URL은 #343/#332의 기존 Parameter를 사용하고 임베딩 Key는 같은 모델 서버의 기존 staging Parameter를 읽기만 한다.

### 최초 Parameter 등록과 적용

Cloud 작업 브랜치의 저장소 루트에서 실행한다. 아래 스크립트는 계정483175530259/서울에 한정하고, 운영 API_KEY/REDIS_URL과 공유 임베딩 Key가 이미 있는지 확인한다. RunPod의 현재 staging URL·Key 두 개를 운영 경로로 처음 한 번 복사한다. 기존 운영 쌍이 있으면 보존하며 한쪽만 있으면 중단한다. 값은 stdout/argv/State에 출력하지 않고0600 임시 요청 파일을 삭제한다. 두 Parameter의 등록은 원자적이지 않으므로 부분 실패 시 자동 덮어쓰기로 복구하지 않는다.

```bash
AWS_PROFILE=yeodam-admin python3 scripts/seed-v2-production-ai-parameters.py
AWS_PROFILE=yeodam-admin terraform -chdir=terraform/v2/production fmt -check
AWS_PROFILE=yeodam-admin terraform -chdir=terraform/v2/production validate
AWS_PROFILE=yeodam-admin terraform -chdir=terraform/v2/production plan -out=/private/tmp/production-ai.plan
# 계정·생성/변경/삭제 및 기존 V1/DB 비변경을 검토한 뒤 같은 Plan 적용
AWS_PROFILE=yeodam-admin terraform -chdir=terraform/v2/production apply /private/tmp/production-ai.plan
```

등록 경로는 `/yeodam/v2/production/ai/VLM_BASE_URL`(String), `/yeodam/v2/production/ai/VLM_API_KEY`(SecureString)다. 모델은 확인한 staging 설정과 같은 `gemma-4`이며 주소는 코드에 고정하지 않는다. 이후 RunPod 변경 시 운영 Parameter를 따로 갱신하고 AI Service의 새 배포가 필요하다. staging URL 갱신용 Actions가 운영 값을 함께 수정하는 구성은 아니다. 키를 CLI 문자열이나 채팅에 넣지 않는다.

```bash
aws ecs describe-services --profile yeodam-admin --region ap-northeast-2 \
  --cluster yeodam-v2-production-app --services yeodam-v2-production-ai-api \
  --query 'services[].{desired:desiredCount,running:runningCount,pending:pendingCount,deployments:deployments[].rolloutState}'
# Parameter 변경 뒤 같은 이미지로 재기동해야 할 때만 실행
aws ecs update-service --profile yeodam-admin --region ap-northeast-2 \
  --cluster yeodam-v2-production-app --service yeodam-v2-production-ai-api \
  --force-new-deployment --query 'service.serviceName' --output text
```

헬스는 `/health`200으로 Redis/Kafka/스토리 실행 루프 상태를 확인한다. 실제 이미지 Pull/IAM/SSM 실패는 ECS stoppedReason과 로그를 구분해서 본다. Target Group의 healthy와 실제 인증 요청은 별도로 확인한다. Rolling은 min100/max200, Circuit Breaker Rollback, ALB deregistration30초, Task stopTimeout120초다. 이 값만으로 실행 중 스토리의 완료나 무손실 교체를 검증했다고 주장하지 않는다. 이미지의 shutdown/drain 처리와 실제 작업 시험은 별도다.

`scripts/verify-v2-production-ai-api.py`는 같은 이미지/Task Role의 일회성 Fargate에서 실행하는 검증 도구다. 허용 시험은 BE SG와 AI Client SG를 함께 붙여 내부 ALB health, 미인증401/인증 metrics200, Redis AUTH, Qdrant readiness, 임베딩·RunPod `/models`와 고유 S3 fixture의 presigned GET을 확인한다. 모델 생성·Kafka 메시지 발행·여행 작업 제출은 하지 않는다. `PROBE_RUN_ID` UUID에 해당하는 `trip-uploads/cloud-preflight-347-<UUID>/derived/read-probe.txt`를 관리자가 만들고 정확한 Version ID만 삭제한다. 비허용 시험은 AI Client SG만 붙이고 `PROBE_MODE=deny`로 DNS 해석 뒤 ALB80 Timeout을 확인한다. DNS 실패/Connection Refused는 SG 차단 성공으로 판정하지 않는다. 예외에는 서명 URL이 포함될 수 있어 타입만 출력한다.

운영 FE/BE 배포·공개 DNS 전환·V1 DB/인증 이전·Outbox Connector 등록은 이 작업에 포함하지 않는다. production AI API는 운영 Kafka/Redis에만 연결하며, staging 서비스나 DB 리허설 Kafka·Consumer Group·Offset을 변경하지 않는다. 운영 AI API/runner의 업무 E2E와 처리량은 사전 배포 헬스 검증과 구분한다. 신규 Service의 자동 배포/복구 CD 연결 역시 후속 운영 배포 작업에서 다룬다.

#347 최초 적용에서는18개 생성·기존 변경/삭제0개였고 후속 전체 Plan은 No changes였다. Service1 Task/rollout COMPLETED, ALB healthy와 위 허용·차단 probe의 종료코드0을 확인했다. fixture Version/DeleteMarker0, GPU 기존6개 컨테이너/boot ID와 공개 DNS·운영 Kafka 업무 offset0/Connector[] 전후 동일을 확인했다. 실제 업무 E2E는 수행하지 않았다.
