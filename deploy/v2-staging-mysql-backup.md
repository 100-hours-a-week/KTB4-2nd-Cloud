# V2 스테이징 MySQL 백업과 Binary Log 복원

## 실행 범위와 현재 상태

Issue #135는 [MySQL 실행 기반](v2-staging-mysql.md)에 `mysqldump` Full Backup, 닫힌 Binary Log의 S3 보존, 전송 지연 관측과 복원 절차를 더한다. 대상은 **스테이징 MySQL 한 개**다. Backend나 AI 코드는 변경하지 않는다. V1 운영 데이터는 스테이징에 복사하지 않고 시험 Schema와 Row로 복원을 확인한다.

이 문서의 설정값은 4단계 설계의 주 1회 Full Backup, 1분 간격 Log 전송, 14일 보존, 전송 성공 지연 3분 경고·5분 위반을 따른다. 2026-10-07 스테이징에 적용하고 작은 시험 데이터의 S3 복원을 확인했다. 목표 RPO 5분은 실제 장애 주입과 복원 결과로 확인하기 전까지 달성했다고 적지 않는다. 아래의 AWS 명령은 재현·운영 절차이며 실제 수행 결과는 뒤의 측정 기록에 구분해 적었다.

## 저장과 일관성

MySQL 컨테이너의 `/var/lib/mysql`은 Host의 별도 Data EBS `/srv/yeodam/mysql`에 마운트돼 있다. `mysql-bin.NNNNNN`도 여기에 생긴다. Host의 systemd Timer가 1분마다 `FLUSH BINARY LOGS`로 현재 파일을 닫고, 닫힌 파일을 **사진 Bucket과 다른** `yeodam-v2-staging-mysql-backup-<account>-ap-northeast-2` Bucket의 `binlog/<server_uuid>/`에 복사한다. S3의 SHA-256 Metadata와 Byte 수를 로컬 파일과 대조한 뒤에만 마지막 성공 시각을 갱신한다. 이름에 `server_uuid`를 넣어 새 MySQL이 같은 `mysql-bin.000001`부터 시작해도 과거 로그를 덮지 않는다. 로컬 Log는 16일 후 만료하도록 설정하고 S3 Backup 및 이전 버전은 14일 후 만료한다.

목요일 03:00 KST에는 `mysqldump --single-transaction --quick --source-data=2`로 `yeodam` Schema를 S3에 압축 스트리밍한다. 시작 시점의 짧은 Global Read Lock으로 Binary Log 파일·위치를 얻고, 그 뒤 InnoDB 일관된 읽기로 백업한다. Backup 중 해당 테이블의 DDL은 피한다. `.sql.gz` 업로드가 끝나고 좌표와 Object 크기를 확인한 뒤에만 같은 이름의 `.ready` Marker를 만든다. **`.ready`가 없는 Dump는 복원 대상으로 선택하지 않는다.** Marker에는 Server UUID, Backup Object Key, 시작 Log 파일·위치와 압축 크기가 있다. 앱 계정과 Secret은 Dump에 넣지 않으므로 복원한 MySQL에서 별도로 준비한다. [MySQL `mysqldump`](https://dev.mysql.com/doc/refman/9.7/en/mysqldump.html)

Data EBS를 잃어도 S3에 전송된 마지막 **연속 Log**까지 복원할 수 있어야 한다. `ExternalBinlogAgeSeconds`는 마지막 완전한 전송 이후 시간을 1분마다 보낸다. CloudWatch Alarm은 180초에 경고, 300초에 전송 기준 초과로 반응하며 지표가 사라져도 비정상으로 판정한다. 이는 전송 상태의 대리 지표이며 **실제 RPO 위반을 직접 측정한 값은 아니다.** 실제 RPO는 복원된 시험 Row의 Commit 시각으로 별도 계산한다. Alarm에는 아직 외부 알림 Action을 연결하지 않았다.

## Terraform 적용 전 확인

명령은 Cloud 저장소 `/Users/lee-y.ch/Desktop/yeodam/KTB4-2nd-Cloud`에서 실행한다. #135 Branch가 최신 `main`을 포함하는지, 작업 트리에 Secret·State·Plan 파일이 없는지 확인한다. Terraform의 `backend.hcl`과 `terraform.tfvars`는 로컬에서 준비하며 Git에 넣지 않는다.

```bash
export AWS_PROFILE=yeodam-admin
aws sts get-caller-identity
terraform fmt -check -recursive terraform/v2
terraform -chdir=terraform/v2/staging validate
terraform -chdir=terraform/v2/staging plan -input=false
```

2026-10-07 실제 계정 `483175530259`, Region `ap-northeast-2`에서 **72개 생성, 변경·삭제 0개** Plan을 적용했다. 앞선 #133까지 62개에 이번 Backup Bucket·설정·두 Alarm·MySQL Role Policy 10개가 더해진 전체 스테이징 Plan이며 V1 Terraform 자원은 이 State에 없다. NAT·ALB·EC2·EBS 등의 상시 비용과 S3·CloudWatch 사용량 비용이 이 시점부터 발생한다. 부팅 오류 수정 뒤 MySQL EC2 User Data만 **1개 제자리 변경**했고 이후 Plan은 변경 없음으로 확인할 예정이다. User Data 변경이 기존 Host에서 스크립트를 다시 실행해 주지는 않아 SSM으로 초기화 상태를 별도로 확인했다.

## 스테이징 적용과 첫 복원 측정 — 2026-10-07

초기 Apply는 72개 생성, 변경·삭제 0개로 끝났다. MySQL EC2 `i-05535ae6ff812972c`와 Data EBS `vol-082934f7a39af5ad1`이 생성됐고, 스테이징 전용 Backup Bucket은 `yeodam-v2-staging-mysql-backup-483175530259-ap-northeast-2`다. Root Secret에는 스테이징 전용 무작위 값을 Secrets Manager에서 설정했다. 값은 Terraform State·Git·이 문서에 넣지 않았다. FE/BE ECS Service는 아직 없고 V1 운영 데이터도 이 DB에 복사하지 않았다.

첫 부팅의 `cloud-init`은 패키지 설치에서 실패했다. EC2가 NAT Gateway와 Private Route보다 먼저 시작돼 Ubuntu 미러 연결이 타임아웃됐고, 경로 완성 뒤에는 같은 미러가 HTTP 200을 반환했다. Terraform MySQL 모듈이 네트워크 모듈 전체를 기다리도록 바꿨다. 재시도에서는 Ubuntu ARM 이미지에 `awscli` APT 후보가 없어 멈췄다. [AWS 공식 Linux ARM 설치 절차](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html)의 ZIP 설치로 User Data를 수정하고 SSM에서 현재 Host를 복구했다. 수정된 User Data의 Terraform Plan은 EC2 1개 제자리 변경, 생성·삭제 0개였고 Apply 뒤 같은 Instance와 Data EBS를 유지했다. 재시작 후 MySQL 서비스, Data EBS 마운트, 세 Timer, 시험 데이터 2건이 남아 있음을 확인했다.

`yeodam.recovery_probe`에 `before` 1건을 기록하고 Full Backup을 수동 실행한 결과 3초가 걸렸으며 S3에는 압축 Dump 1,008바이트와 `.ready` Marker 193바이트가 생겼다. 이후 `after` 1건을 기록하고 닫힌 Binary Log를 S3에 전송했다. 복원 시험은 S3에서 Dump와 연속된 Log 4개를 내려받아 각 Log의 S3 SHA-256 Metadata·크기를 대조했다. 별도 네트워크 미연결 MySQL 컨테이너에 Dump만 적용했을 때 1건, Log 재생 뒤에는 `before,after` 2건이었다. **다운로드 시작부터 별도 DB의 Row 검증까지 28.561초**가 걸렸다. 이 수치는 작은 시험 데이터의 **DB 단독 복원 시간**이며 새 EC2 생성, 장애 감지, DNS·BE 재연결은 포함하지 않는다.

`ExternalBinlogAgeSeconds`가 1분 주기로 발행됐고 초기 전송 전 두 Alarm은 `ALARM`, 전송 뒤에는 `OK`로 바뀌었다. 정상 구간 표본 최대치는 47~59초였다. 첫 발행 값은 마지막 성공 시각 파일이 없어 Epoch부터 계산된 초기값이므로 실제 전송 지연 표본으로 사용하지 않는다. **Alarm Action이 없어 Discord나 SNS 알림은 전송되지 않는다.** 현재 CloudWatch는 PLG 도입 전 이 백업 경로를 관측하기 위한 임시 수단이며, PLG의 지표·알림 경로가 준비되면 유지 여부를 재검토한다.

이번 시험은 실제 Data EBS 손실이나 새 Host 전환이 아니므로 **RPO 5분 달성 및 전체 RTO를 입증하지 않는다.** 실제 장애 시점과 마지막 복원 Commit을 기록하는 시험, 운영 규모의 Dump 크기·복원 시간, 백업 중 쓰기 지연·오류율은 후속 측정으로 남긴다.

## 적용 후 백업 확인

먼저 [MySQL 실행 문서](v2-staging-mysql.md)에 따라 Root Secret을 넣고 MySQL 서비스를 정상화한다. `yeodam` Schema가 없다면 스테이징 시험용으로 생성한다. 아래 명령은 **SSM으로 스테이징 MySQL EC2에 접속한 Host Shell**에서 실행한다. 이 Host에는 Instance Role이 있으므로 AWS Profile을 넣지 않는다.

```bash
sudo systemctl status yeodam-mysql.service
sudo systemctl list-timers --all 'yeodam-mysql-*'
sudo docker exec yeodam-v2-mysql sh -ec 'export MYSQL_PWD="$(cat /run/secrets/mysql-root-password)"; mysql -uroot -e "CREATE DATABASE IF NOT EXISTS yeodam; SHOW BINARY LOG STATUS;"'
sudo systemctl start yeodam-mysql-ship-binlogs.service
sudo systemctl start yeodam-mysql-full-backup.service
sudo journalctl -u yeodam-mysql-ship-binlogs.service -u yeodam-mysql-full-backup.service --since '15 minutes ago' --no-pager
```

Terraform Output `mysql_backup_bucket_name`의 값으로 아래 `<bucket>`을 바꾼다. `full/`에는 `.sql.gz`와 같은 시각의 `.ready`가 함께 있어야 하고, `binlog/<uuid>/`에는 닫힌 Log가 있어야 한다. 첫 Backup 크기, 소요 시간, Backup 동안 쓰기 p95 및 오류, Binlog 전송 지연과 S3 저장량을 기록한다. 실패 시 `systemctl status`, 위 Journal과 `aws s3api head-object`의 권한·Object Metadata, NAT/HTTPS 경로를 확인한다.

```bash
aws s3 ls s3://<bucket>/full/
aws s3 ls s3://<bucket>/binlog/ --recursive
sudo cat /var/lib/yeodam/mysql-binlog-last-uploaded
sudo cat /var/lib/yeodam/mysql-binlog-last-success
```

## 별도 MySQL로 시점 복원

이 절차는 스테이징 **시험용 새 MySQL 컨테이너**를 사용한다. 기존 `yeodam-v2-mysql`의 Data EBS와 Private DNS는 건드리지 않는다. 시험용 컨테이너는 새 Docker Volume에 데이터를 두고 포트를 공개하지 않는다. 이 시험의 RTO는 Dump 다운로드·적용·Binary Log 재생·검증까지이며, 별도 EC2 생성과 DNS 전환 시간은 포함하지 않는다. Data EBS 손실 대응의 전체 RTO는 후속 별도 Host 복원 시험에서 측정해야 한다.

1. `full/<stamp>.ready`를 내려받아 `backup_key`, `server_uuid`, `source_file`, `source_position`을 기록한다. 같은 이름의 `.sql.gz`가 존재하고 `compressed_bytes`가 `head-object`의 ContentLength와 맞는지 확인한다.
2. Marker의 `server_uuid` Prefix에서 `source_file`부터 가장 최근의 **닫힌** Binary Log까지 내려받는다. 파일 번호가 연속인지, 각 S3 Object의 `Metadata.sha256` 및 ContentLength가 내려받은 파일과 같은지 확인한다. 하나라도 빠지거나 다르면 적용을 중단한다.
3. 새 MySQL 컨테이너에 `gzip -t`로 검사한 Dump를 적용한다. 이때는 Backup 시점 이전의 시험 Row만 있어야 한다.
4. 버전이 맞는 `mysqlbinlog`로 첫 파일은 `source_position`부터, 나머지 파일은 순서대로 **하나의 MySQL 연결**에서 적용한다. 현재 고정한 MySQL 이미지에는 `mysqlbinlog`가 PATH에는 없지만 `/usr/libexec/mysqlsh/mysqlbinlog`에 있는 것을 로컬에서 확인했다. 실제 Host에서도 버전을 확인한다. [MySQL 시점 복구](https://dev.mysql.com/doc/refman/9.7/en/point-in-time-recovery-binlog.html)
5. Backup 뒤에 기록한 시험 Row까지 들어왔는지, 사용자·여행·사진 메타데이터와 작업 상태가 예상 수와 일치하는지 확인한다. `RPO = 장애 기준 시각 − 마지막으로 복원된 Commit 시각`, `RTO = 장애 감지부터 BE가 정상 요청을 받기까지`로 기록한다. 이 컨테이너 단독 시험에서 후자는 전체 RTO가 아니므로 별도 표시한다.

다음은 위 순서를 실행하는 스테이징 예시다. **SSM의 DB Host Shell**에서 실행하고 `<bucket>`과 `<stamp>`는 `aws s3 ls`로 확인한 값으로 바꾼다. `ready` 파일을 내려받은 뒤 기록된 좌표와 UUID를 눈으로 확인한다. 시험용 Volume 이름은 매번 새로 만들고 기존 운영 Container 이름을 넣지 않는다.

```bash
BACKUP_BUCKET='<bucket>'
BACKUP_STAMP='<stamp>'
RESTORE_DIR=$(mktemp -d)
aws s3 cp "s3://${BACKUP_BUCKET}/full/${BACKUP_STAMP}.ready" "$RESTORE_DIR/ready"
cat "$RESTORE_DIR/ready"
```

`ready`의 `backup_key`, `server_uuid`, `source_file`, `source_position`을 아래 변수에 **확인한 그대로** 입력한다. Marker에 적힌 크기와 S3 크기가 같아야 한다. `aws s3 sync`는 해당 Server UUID의 Log만 별도 디렉터리로 내려받는다. 다른 Server UUID의 Log를 이어 붙이지 않는다.

```bash
BACKUP_KEY='full/<stamp>.sql.gz'
SERVER_UUID='<ready의 server_uuid>'
SOURCE_FILE='<ready의 source_file>'
SOURCE_POS='<ready의 source_position>'
aws s3api head-object --bucket "$BACKUP_BUCKET" --key "$BACKUP_KEY" --query ContentLength --output text
aws s3 cp "s3://${BACKUP_BUCKET}/${BACKUP_KEY}" "$RESTORE_DIR/full.sql.gz"
gzip -t "$RESTORE_DIR/full.sql.gz"
mkdir "$RESTORE_DIR/binlog"
aws s3 sync "s3://${BACKUP_BUCKET}/binlog/${SERVER_UUID}/" "$RESTORE_DIR/binlog/" --only-show-errors
```

`SOURCE_FILE`이 존재하는지 확인한 뒤 그 파일부터 정렬한다. 파일 번호가 하나라도 건너뛰면 아래 명령은 중단한다. 각 파일의 SHA-256 Metadata와 크기도 S3 `head-object` 및 로컬 `sha256sum`·`stat`으로 대조한다. 이는 Download 손상과 잘못된 복원 세트를 거르는 필수 확인이다. `BINLOG_FILES` 배열에는 검증된 Log만 순서대로 남긴다.

```bash
set -Eeuo pipefail
[[ "$SOURCE_FILE" =~ ^mysql-bin\.([0-9]+)$ ]]
expected=$((10#${BASH_REMATCH[1]}))
[[ "$SOURCE_POS" =~ ^[0-9]+$ ]]
mapfile -t DOWNLOADED_LOGS < <(find "$RESTORE_DIR/binlog" -maxdepth 1 -type f -name 'mysql-bin.*' -print | sort -V)
BINLOG_FILES=()
for path in "${DOWNLOADED_LOGS[@]}"; do
  filename=${path##*/}
  number=$((10#${filename##*.}))
  (( number < expected )) && continue
  (( number == expected )) || { echo "Binary Log 누락: $expected" >&2; exit 1; }
  remote_sha=$(aws s3api head-object --bucket "$BACKUP_BUCKET" --key "binlog/${SERVER_UUID}/${filename}" --query Metadata.sha256 --output text)
  remote_size=$(aws s3api head-object --bucket "$BACKUP_BUCKET" --key "binlog/${SERVER_UUID}/${filename}" --query ContentLength --output text)
  local_sha=$(sha256sum "$path" | awk '{print $1}')
  local_size=$(stat -c %s "$path")
  [[ "$remote_sha" == "$local_sha" && "$remote_size" == "$local_size" ]] || { echo "Binary Log 검증 실패: $filename" >&2; exit 1; }
  BINLOG_FILES+=("/backup/${filename}")
  expected=$((expected + 1))
done
(( ${#BINLOG_FILES[@]} > 0 )) || { echo '복원 가능한 닫힌 Binary Log가 없음' >&2; exit 1; }
printf '%s\n' "${BINLOG_FILES[@]}"
```

검증된 Log 목록만 복원한다. **`SOURCE_POS`가 첫 파일 이후 변경을 정확히 이어주는지** 시험 Row로 재확인한다. 아래 `IMAGE`는 #133에서 고정한 이미지다. Root 암호는 Command 인자에 쓰지 않고 Host의 Root 전용 파일을 컨테이너에 읽기 전용으로 전달한다.

```bash
IMAGE='mysql:9.7.2@sha256:e2bde46db6563855d7177adb5f0b57b9dc663f5a20927a90f4259d3312068497'
RESTORE_CONTAINER="yeodam-restore-$(date -u +%Y%m%d%H%M%S)"
RESTORE_VOLUME="${RESTORE_CONTAINER}-data"
sudo docker volume create "$RESTORE_VOLUME"
sudo docker run -d --name "$RESTORE_CONTAINER" --network none \
  -v "$RESTORE_VOLUME:/var/lib/mysql" \
  -v /etc/yeodam/mysql-root-password:/run/secrets/mysql-root-password:ro \
  -e MYSQL_ROOT_PASSWORD_FILE=/run/secrets/mysql-root-password \
  "$IMAGE" --log-bin=mysql-bin
sudo docker exec "$RESTORE_CONTAINER" sh -ec 'export MYSQL_PWD="$(cat /run/secrets/mysql-root-password)"; mysql -uroot -NBe "SELECT 1"'
gzip -dc "$RESTORE_DIR/full.sql.gz" | sudo docker exec -i "$RESTORE_CONTAINER" sh -ec 'export MYSQL_PWD="$(cat /run/secrets/mysql-root-password)"; exec mysql -uroot --binary-mode'
```

복원 Container가 준비되지 않았다면 `sudo docker logs "$RESTORE_CONTAINER"`를 보고 `SELECT 1`부터 다시 실행한다. `BINLOG_FILES`는 앞 단계에서 만든 배열이다. 아래 파이프라인은 `set -o pipefail`을 켜고 실행해 `mysqlbinlog`나 `mysql` 오류를 놓치지 않는다.

```bash
set -o pipefail
sudo docker run --rm --network none -v "$RESTORE_DIR/binlog:/backup:ro" "$IMAGE" \
  /usr/libexec/mysqlsh/mysqlbinlog --start-position="$SOURCE_POS" "${BINLOG_FILES[@]}" \
  | sudo docker exec -i "$RESTORE_CONTAINER" sh -ec 'export MYSQL_PWD="$(cat /run/secrets/mysql-root-password)"; exec mysql -uroot --binary-mode'
sudo docker exec "$RESTORE_CONTAINER" sh -ec 'export MYSQL_PWD="$(cat /run/secrets/mysql-root-password)"; mysql -uroot -e "SHOW DATABASES;"'
```

검증이 끝난 뒤에만 `sudo docker rm -f "$RESTORE_CONTAINER"`와 `sudo docker volume rm "$RESTORE_VOLUME"`으로 **시험용** 자원을 지운다. 운영 Container와 Data EBS는 삭제하지 않는다. 실패 시 시험 Volume을 보존해 원인을 조사하고, 새 이름으로 처음부터 다시 복원한다.

로컬에서는 `bash scripts/test-v2-mysql-backup-local.sh`가 같은 이미지의 **서로 다른 두 컨테이너**로 Full Backup의 1건과 그 뒤 Binary Log에 기록된 1건을 복원한다. 실제 S3 전송·Timer·Alarm과 EBS 손실 복원은 이 로컬 시험으로 검증했다고 보지 않는다.
