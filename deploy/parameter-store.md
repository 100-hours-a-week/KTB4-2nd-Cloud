# V1 Parameter Store Runtime 설정 계약

운영 환경변수는 Git, Terraform 변수와 Terraform state에 저장하지 않는다. App과 Worker EC2가 자신의 Instance Role로 Parameter Store를 조회하고 `/run/yeodam` 아래에 권한 `600`인 임시 env 파일을 생성한다.

```text
Parameter Store → EC2 Instance Role → /run/yeodam/{scope}.env
                → Docker Compose → Container environment
```

EC2를 재부팅하면 `/run`의 파일은 사라진다. 배포 스크립트는 Compose 실행 전 `render-runtime-env.sh`를 다시 실행해야 한다. Parameter 값을 변경한 경우에도 env 파일을 다시 만들고 해당 Container를 재생성해야 새 값이 적용된다.

## Parameter 경로와 Type

App EC2는 `/yeodam/v1/app/*`, Worker EC2는 `/yeodam/v1/worker/*`만 조회한다. 두 EC2는 Image Pull에 필요한 `/yeodam/v1/deploy/*`를 함께 조회한다.

### App

| Name | Type | 필수 |
| --- | --- | --- |
| `MYSQL_URL` | `String` | O |
| `MYSQL_USERNAME` | `String` | O |
| `MYSQL_PASSWORD` | `SecureString` | O |
| `AWS_REGION` | `String` | O |
| `ATTACHMENT_S3_BUCKET` | `String` | O |
| `AI_SERVER_BASE_URL` | `String` | O |
| `AI_SERVER_API_KEY` | `SecureString` | O |
| `AI_EC2_INSTANCE_ID` | `String` | O |
| `FRONTEND_OAUTH_CALLBACK_URI` | `String` | O |
| `KAKAO_CLIENT_ID` | `String` | O |
| `KAKAO_REDIRECT_URI` | `String` | O |
| `KAKAO_CLIENT_SECRET` | `SecureString` | O |
| `JWT_SECRET` | `SecureString` | O |
| `JWT_ISSUER` | `String` | O |
| `FRONTEND_ORIGIN` | `String` | O |
| `DATA_GO_KR_SERVICE_KEY` | `SecureString` | 선택, 미등록 시 빈 값 |
| `DATA_GO_KR_LEGAL_DISTRICT_API_URL` | `String` | O |
| `DATA_GO_KR_TIMEOUT` | `String` | 선택, 기본값 `10s` |

전체 경로는 `/yeodam/v1/app/{Name}`이다.

### Worker

| Name | Type | 필수 |
| --- | --- | --- |
| `API_KEY` | `SecureString` | O |
| `S3_BUCKET` | `String` | O |
| `FAKE_PIPELINE` | `String` | 선택, 기본값 `0` |
| `LOG_LEVEL` | `String` | 선택, 기본값 `INFO` |

전체 경로는 `/yeodam/v1/worker/{Name}`이다. `API_KEY`는 App의 `AI_SERVER_API_KEY`, `S3_BUCKET`은 App의 `ATTACHMENT_S3_BUCKET`과 같은 값을 사용한다.

### 배포 인증

| 전체 경로 | Type | 용도 |
| --- | --- | --- |
| `/yeodam/v1/deploy/GHCR_USERNAME` | `String` | GHCR 로그인 사용자 |
| `/yeodam/v1/deploy/GHCR_TOKEN` | `SecureString` | `read:packages`만 가진 Image Pull PAT |

배포 인증 값은 Container 환경변수 파일에 넣지 않는다. 후속 배포 스크립트가 Image Pull 직전에 조회해 `docker login --password-stdin`에 전달하고 임시 인증 정보를 제거한다.

## 값 등록과 변경

AWS Console에서는 Systems Manager → Parameter Store → Create parameter에서 경로, Type과 값을 입력한다. Secret은 반드시 `SecureString`, 일반 설정은 `String`으로 만든다. Terraform은 조회 권한만 관리하며 실제 Parameter 값은 관리하지 않는다.

AWS CLI로 Secret을 등록하거나 변경할 때는 값을 명령 기록에 직접 쓰지 않는다.

```bash
read -rsp 'MYSQL_PASSWORD: ' YEODAM_PARAMETER_VALUE
echo

aws ssm put-parameter \
  --region ap-northeast-2 \
  --name /yeodam/v1/app/MYSQL_PASSWORD \
  --type SecureString \
  --value "$YEODAM_PARAMETER_VALUE" \
  --overwrite

unset YEODAM_PARAMETER_VALUE
```

`put-parameter --overwrite`는 새 Version을 만든다. 변경 전후 Version과 수정 시각은 Console 또는 `describe-parameters`로 확인하되, Secret 값을 터미널이나 작업 로그에 출력하지 않는다.

## EC2에서 Runtime env 생성

EC2에는 AWS CLI와 Python 3가 필요하며 App/Worker Host bootstrap이 이를 준비한다. 스크립트는 예상하지 않은 Parameter, 잘못된 Type, 필수값 누락과 여러 줄 값을 거부하고 실제 값은 출력하지 않는다.

```bash
sudo ./scripts/render-runtime-env.sh --scope app
sudo ./scripts/render-runtime-env.sh --scope worker
```

생성 결과만 확인한다.

```bash
sudo stat -c '%U:%G %a %n' /run/yeodam/app.env
sudo cut -d= -f1 /run/yeodam/app.env
```

Runtime 값과 Cloud Commit에 고정한 Image 버전을 별도 파일로 전달한다.

```bash
docker compose \
  --env-file /run/yeodam/app.env \
  --env-file /opt/yeodam/releases/RELEASE_SHA/deploy/image-versions.env \
  --file /opt/yeodam/releases/RELEASE_SHA/app-compose.yml \
  up --detach
```

`image-versions.env`에는 Secret을 넣지 않고 FE/BE/AI의 Commit SHA Tag와 Digest만 저장한다.
