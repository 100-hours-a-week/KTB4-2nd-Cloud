# AI 외부 장소 참조 데이터

스테이징·운영 정리 실행기가 공통으로 읽는 외부 장소 데이터를 다음 경로에 저장한다.

```text
s3://yeodam-v1-app-data-483175530259-ap-northeast-2/ai-reference/places/
```

기존 Private S3 버킷의 암호화·버전 관리·퍼블릭 차단을 유지한다. 사진 경로의 권한을 넓히거나 새 버킷을 만들지 않는다. 실제 데이터의 파일 이름·형식·크기는 AI 담당자가 정한다.

| 주체 | 허용 범위 |
| --- | --- |
| AI SSO `YeodamAIWorkerOperator` | 위 prefix의 목록·업로드·읽기, multipart 업로드 중단·파트 목록 |
| 공유 GPU 역할 `yeodam-v2-staging-ai-gpu` | 같은 prefix의 목록·읽기 |

새 권한에 객체 삭제·ACL 변경·버킷 전체 목록·사진 접근은 포함하지 않는다. GPU의 기존 사진 읽기 권한은 보존한다. 두 정리 실행기는 같은 EC2 역할을 쓰므로 이 공통 데이터에 대한 컨테이너별 IAM 격리는 아니다.

## AI 담당자 업로드

본인의 AWS CLI SSO 프로필을 `ai-admin`이라고 가정한 예시다. 프로필 이름이 다르면 바꾼다. 명령은 파일이 있는 로컬 터미널에서 실행한다. S3 콘솔의 버킷 전체 탐색 권한은 추가하지 않으므로 CLI의 정확한 URI로 접근한다.

```bash
aws sso login --profile ai-admin
aws sts get-caller-identity --profile ai-admin
# 로컬 파일과 버전 경로는 실제 데이터에 맞춰 변경한다.
aws s3 cp ./places-data.json \
  s3://yeodam-v1-app-data-483175530259-ap-northeast-2/ai-reference/places/v1/places-data.json \
  --profile ai-admin --region ap-northeast-2
aws s3 ls \
  s3://yeodam-v1-app-data-483175530259-ap-northeast-2/ai-reference/places/v1/ \
  --profile ai-admin --region ap-northeast-2
```

업로드 완료 메시지와 목록의 파일 크기를 확인한다. 큰 파일은 CLI가 multipart로 보낼 수 있다. `--acl`이나 동기화의 `--delete`를 사용하지 않는다. 데이터는 `v1/`, `v2/`처럼 구분하고 실행기가 읽을 경로를 명시한다. 이미 사용하는 버전 경로를 덮어쓰기보다는 새 버전을 올린다. 잘못 올린 객체의 삭제는 관리자에게 정확한 key와 version ID를 전달한다.

`AccessDenied`이면 STS 역할이 해당 Permission Set인지, URI가 정확한지, 업로드에 ACL 옵션이 없는지 확인한다. Permission Set 변경 직후에는 SSO 로그인을 갱신하고 다시 확인한다. 실제 AI 사용자 세션의 업로드는 본인이 확인해야 하며 관리자 정책 시뮬레이션과 구분한다.

## 실행기 시작 시 다운로드

권한만으로 다운로드가 자동 실행되지는 않는다. AI 코드에서 사용할 버전 URI·로컬 경로·다운로드 실패 시 동작을 연결해야 한다. 아래는 기존 컨테이너에서 AWS SDK의 기본 자격 증명 체인(EC2 역할)을 쓰는 예시이며 장기 키를 환경 변수로 넣지 않는다.

```python
import boto3

boto3.client('s3', region_name='ap-northeast-2').download_file(
    'yeodam-v1-app-data-483175530259-ap-northeast-2',
    'ai-reference/places/v1/places-data.json',
    '/tmp/places-data.json',
)
```

파일 크기에 따른 로컬 디스크 여유와 최초 다운로드 시간, 캐시·버전 확인, 다운로드 실패 시 재시도/기동 정책은 실제 데이터를 기준으로 AI가 정한다. 이 Cloud 작업은 운영 컨테이너의 command·이미지·환경 변수를 변경하거나 재시작하지 않는다.

## Cloud 권한 적용과 원복

Cloud 저장소 루트에서 관리자 SSO로 실행한다. `policies.json`이 SSO와 Terraform의 공통 권한 정의다. 기존 Identity Center Permission Set은 Terraform 소유가 아니므로 전용 도구로 필요한 문장만 추가한다. AWSReservedSSO 역할을 직접 수정하지 않는다.

```bash
python3 scripts/configure-ai-reference-sso.py
# 출력의 beforeSha256를 검토해 입력한다. backup-dir은 Git 밖 비공개 경로다.
python3 scripts/configure-ai-reference-sso.py --apply \
  --expected-policy-sha256 '<beforeSha256>' \
  --backup-dir /private/tmp/yeodam-ai-reference-private
```

도구는 계정·Permission Set 이름과 해시를 확인하고 기존 정책을 0600 파일로 보존한다. 현재 정책을 재조회한 후 전용 Sid만 추가하며 해당 계정만 재프로비저닝한다. 같은 Sid에 다른 내용이 있거나 해시가 바뀌었으면 중단한다. AWS에는 원자적 정책 CAS API가 없으므로 적용 동안 다른 관리자의 동일 Permission Set 변경을 피한다. 실패·대기 시 출력된 request ID로 상태를 확인하고 전체 정책을 무조건 덮어쓰지 않는다.

실행기 읽기 정책은 `terraform/v2/production/ai-reference-data.tf`의 별도 named policy다. 기존 production Terraform 초기화 절차를 따라 fmt → validate → plan으로 확인한 저장 Plan만 적용한다. 기대 변경은 `aws_iam_role_policy.ai_reference_data_read` 생성 1개, 변경·삭제 0개다. 다른 자원 변경이 나오면 함께 적용하지 않는다. 외부 변경 때문에 필요한 경우에만 이 정책 대상으로 별도 Plan을 만들고 근거를 기록한다. ECS·EC2 재시작은 필요 없다.

SSO 원복은 `--remove`로 Plan을 먼저 확인한 후 같은 옵션에 `--apply`, 현재 `beforeSha256`, `--backup-dir`을 준다. 도구가 소유한 정확한 세 문장만 제거한다. 실행기 권한 원복은 해당 Terraform resource 제거 PR과 검토한 Plan으로 그 named policy만 삭제한다. 다른 S3/SSM/로그 정책은 건드리지 않는다. 공유 GPU의 기존 사진 권한으로도 이 prefix를 읽을 수 없는지는 별도로 확인한다.

## 검증 경계

- 정책 병합·충돌·동시 변경 방어 및 권한 범위는 `tests/ci/test_ai_reference_access.py`로 확인한다.
- 적용 후 IAM 시뮬레이션으로 허용 prefix와 다른 prefix·삭제·실행기 쓰기를 구분한다. 기존 SSM/사진 읽기 허용도 대조한다.
- 관리자 소량 시험 객체를 두 실행기에서 기본 역할로 읽어 SHA256를 비교한다. 운영 사진·참조 실데이터를 내려받지 않는다. 시험 객체는 생성한 version ID만 관리자가 정리한다.
- IAM 시뮬레이션 성공은 AI 사용자의 실제 로그인·업로드 성공이나 AI 앱의 시작 시 다운로드 구현 완료를 뜻하지 않는다.
