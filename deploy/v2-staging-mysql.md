# V2 스테이징 MySQL 실행 기반

2026-10-07 Issue #133은 MySQL을 FE/BE Task와 분리된 스테이징 EC2에서 실행하도록 Terraform과 초기화 절차를 작성한다. 이 문서의 결과는 **코드와 실제 AWS 계정 Plan 검증**이다. 현재 스테이징 AWS 자원은 적용하지 않았고, MySQL 기동·접속·복구시간도 측정하지 않았다. 다음 Issue에서 Full Backup, Binary Log 외부 보존과 실제 복원을 함께 구현·검증한다.

## 승인 설계와 이번 구현

4단계 승인 설계의 Single-AZ `t4g.medium`, 암호화된 별도 120GiB gp3 Data EBS, BE 전용 DB 접근, Process 재시작, Private DNS와 Binary Log 기준을 유지했다. 기존 [스테이징 네트워크](v2-staging-network.md)의 가용 영역 A Private Subnet을 사용하며 공인 IP와 SSH Ingress는 없다. Root EBS 30GiB는 Instance 종료 시 삭제되지만 **Data EBS는 별도 Terraform 자원**이고 `prevent_destroy`가 설정돼 있다. EC2를 교체할 때 Data Volume ID와 내용을 그대로 사용할 수 있어야 하며, 자동 복원 성공으로 간주하지 않고 실제 재연결을 시험한다.

V1 App Host는 Ubuntu x86에 MySQL 9.7을 APT로 설치한다. 검토한 공식 Ubuntu noble MySQL 9.7 APT 인덱스에는 `binary-amd64`만 보여, `t4g` ARM 호스트에 동일한 설치 절차를 적용할 근거가 없었다. [MySQL APT 인덱스](https://repo.mysql.com/apt/ubuntu/dists/noble/mysql-9.7-lts/) 대신 ARM64를 포함하는 [공식 MySQL 9.7.2 이미지](https://hub.docker.com/_/mysql/tags)를 EC2에서 실행한다. 2026-10-07 `docker buildx imagetools inspect`로 `linux/arm64/v8`과 Image Index Digest `sha256:e2bde46db6563855d7177adb5f0b57b9dc663f5a20927a90f4259d3312068497`를 확인했다. 이 **스테이징 구현 차이**는 운영 DB 방식에 자동 적용하지 않는다. MySQL 프로세스는 Docker Container 안에서 실행하지만 systemd가 Container 재시작을 관리하고 데이터는 EC2 외부 Data EBS에 남는다.

AMI는 Canonical 소유 계정의 서울 리전 Ubuntu 24.04 ARM64 `ami-0ccbfe1123f2d682d`를 2026-10-07 조회해 고정했다. 바꿀 때는 새 AMI의 발행자·아키텍처와 Instance 교체 Plan을 검토한다. Instance Role에는 SSM 기본 관리 권한과 **이 환경의 MySQL Root Secret 읽기**만 부여한다. Secret 저장 위치만 Terraform이 만들고 값은 넣지 않는다. SSM과 AWS Secrets Manager 접근은 Private Subnet에서 NAT를 통한다.

## 데이터 볼륨, 인증과 접속 경계

초기화 스크립트는 지정된 EBS Volume ID의 Nitro 장치를 Serial로 찾아 연결을 기다린다. 파일시스템이 없을 때만 ext4로 포맷하고 기존 ext4는 다시 포맷하지 않는다. `/srv/yeodam/mysql`에 UUID로 마운트하며, 잘못된 파일시스템이면 시작을 중단한다. systemd는 이 준비 단계와 Secret 로딩이 끝난 뒤 MySQL Container를 시작한다. Root Secret은 평문 값으로 AWS Secrets Manager에 입력하며, Terraform 변수·State·Plan에 넣지 않는다. 호스트가 받아온 값은 Root만 읽는 파일로 보관하고 Container에는 읽기 전용 파일로 전달한다. Root 계정은 로컬 접속으로 제한한다. Secret 변경만으로 기존 MySQL 계정 암호가 자동 회전되지는 않는다.

새 BE Task Security Group에서만 MySQL Security Group의 3306 Ingress를 허용한다. BE Security Group은 현재 DB 3306과 외부 HTTPS Egress만 정의했고, ALB Ingress·Redis·Queue 경로는 후속 Service 작업에서 추가한다. FE Task와 인터넷에서 직접 MySQL에 접근할 수 없다. 여러 BE가 이 Security Group을 공유하더라도 DB 데이터와 연결은 별도 MySQL Instance에 남는다.

Route 53 Private Hosted Zone `staging.yeodam.internal`을 정의했다. **`mysql.staging.yeodam.internal` A Record는 처음에는 만들지 않는다.** DB 데이터와 접속을 확인한 뒤 로컬 `terraform.tfvars`의 `mysql_active_private_ip`를 검증한 Private IP로 설정하고 다시 Plan/Apply해야 이름이 연결된다. EC2 교체나 Backup 복원 시에도 새 DB의 데이터가 확인되기 전에는 DNS 대상이 자동으로 바뀌지 않는다. 기존 Connection Pool은 DNS 변경만으로 이동하지 않으므로 BE 재연결을 별도 시험한다.

MySQL Container에는 `log-bin`, `sync_binlog=1`, `innodb_flush_log_at_trx_commit=1`을 설정했다. 이는 로컬 Binary Log와 Commit 내구성의 **시작 설정**이다. #135에서 [Full Backup과 닫힌 Log의 S3 외부 보존](v2-staging-mysql-backup.md) 코드를 더했다. 실제 적용과 복원 결과가 나오기 전에는 4단계 설계의 RPO 5분을 달성했다고 말할 수 없다.

## 적용 절차와 검증

명령은 Cloud 저장소 루트 `/Users/lee-y.ch/Desktop/yeodam/KTB4-2nd-Cloud`에서 실행한다. `backend.hcl`과 실제 `terraform.tfvars`는 Git에서 제외한다.

```bash
export AWS_PROFILE=yeodam-admin
aws sts get-caller-identity
terraform fmt -check -recursive terraform/v2
terraform -chdir=terraform/v2/staging init -backend-config=backend.hcl
terraform -chdir=terraform/v2/staging validate
terraform -chdir=terraform/v2/staging plan -input=false
```

2026-10-07 실제 계정의 미적용 State Plan은 **62개 생성, 변경 0, 삭제 0**이다. 앞선 네트워크·ALB·ECS·사진 저장소 46개와 이번 MySQL 관련 16개이며 V1 자원 변경·삭제는 없다. 적용 전 대상 계정 `483175530259`, 리전 `ap-northeast-2`, ARM AMI, DB Instance·EBS 용량, NAT/EC2/EBS 상시 비용, 생성/변경/삭제 수를 다시 확인한다. 4단계 설계는 `t4g.medium`과 120GiB Data EBS 및 30GiB Root EBS의 730시간 예상을 약 $44/월로 제시했지만, 현재 가격과 스테이징의 실제 실행시간은 적용 전에 다시 계산해야 한다. 네트워크·ALB 비용과 EBS Snapshot/Backup은 이 금액에 포함되지 않는다.

AWS 적용 후 Terraform Output에서 Instance ID, Private IP, Data Volume ID와 Root Secret ARN을 확인한다. Secret에는 이 환경 전용 강한 **평문 Root 암호**를 AWS Console에서 입력한다. Secret에 아직 값이 없으면 DB systemd 서비스는 시작하지 못하고 재시도한다. 값이 들어간 뒤 SSM Session Manager로 DB EC2에 접속해 다음을 확인한다.

```bash
sudo systemctl status yeodam-mysql.service
findmnt /srv/yeodam/mysql
sudo docker ps --filter name=yeodam-v2-mysql
sudo docker exec -it yeodam-v2-mysql mysql -uroot -p
```

MySQL에서 `SELECT VERSION();`, `SHOW VARIABLES LIKE 'log_bin';`, `SHOW VARIABLES LIKE 'sync_binlog';`을 확인한다. 시험 Schema/Row를 기록한 뒤 `sudo systemctl restart yeodam-mysql.service`로 같은 Data EBS에서 값이 유지되는지 확인한다. Secret 누락은 `journalctl -u yeodam-mysql`, EBS 발견·마운트 오류는 `lsblk -o NAME,SERIAL,FSTYPE`와 `findmnt`, Image Pull 실패는 NAT/443과 Docker Hub 접근을 점검한다. 아직 실행하지 않았으므로 위 결과는 측정 예정이다. BE의 실제 연결은 FE/BE Service와 앱 계정이 준비된 뒤 검증한다.
