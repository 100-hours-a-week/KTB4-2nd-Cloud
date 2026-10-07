# V2 스테이징 사진 저장소와 접근 경계

2026-10-07 Issue #131은 운영 사진과 분리된 스테이징 S3 Bucket과 BE Task의 사진 접근 권한을 Terraform에 정의한다. [스테이징 네트워크](v2-staging-network.md)의 S3 Gateway Endpoint는 **VPC에서 S3로 가는 경로**이고, 이 문서의 Bucket은 **사진을 보존하는 새 저장소**다. FE/BE 실행 경로, 브라우저 PUT과 AWS 자원 적용은 아직 검증하지 않았다.

## 새로 정의한 자원과 기존 자원의 관계

새 Bucket 이름은 `yeodam-v2-staging-app-data-<AWS_ACCOUNT_ID>-ap-northeast-2`다. 운영 V1 Bucket을 재사용하거나 그 안의 Prefix만 나눠 쓰지 않는다. Bucket은 같은 AWS 계정의 S3 서비스에 있지만 별도 이름, Terraform State와 정책을 가진다. 실제 계정 `483175530259`에서 예상하는 이름의 Bucket은 2026-10-07 `HeadBucket` 조회에 404를 반환했다. 글로벌 Bucket 이름의 최종 사용 가능 여부는 생성 시 다시 확인된다.

Bucket은 Public Access Block 네 항목을 모두 켜고, Object Ownership을 `BucketOwnerEnforced`로 둔다. 기본 암호화는 SSE-S3(AES256), Versioning은 Enabled다. Bucket Policy는 HTTP 접근을 거부한다. `prevent_destroy`로 실수에 의한 Terraform 삭제를 막는다. 이는 운영 Bucket 설정을 스테이징에 맞게 재현한 것이며, **AWS 적용 전의 코드 상태**다.

브라우저 CORS는 `https://staging.yeodam-2gether.com`의 `PUT`과 `Content-Type`, `If-None-Match`만 허용한다. 이는 현재 BE의 Presigned PUT 요청 Header와 V1 Cloud의 직접 업로드 CORS 계약을 기준으로 했다. `OPTIONS` Preflight 응답은 S3가 CORS 규칙에 따라 처리하며 `OPTIONS`를 `allowed_methods`에 추가하지 않는다. 로컬 개발 Origin과 운영 Origin은 스테이징 Bucket CORS에 넣지 않았다. CORS는 브라우저 제한이지 S3 권한 부여가 아니다.

## 누가 Object에 접근하는가

FE Task에는 S3 IAM 권한을 주지 않는다. 브라우저가 BE에서 받은 Presigned URL로 S3에 직접 PUT한다. URL을 발급하는 BE에는 **Task Execution Role이 아닌 별도 BE Task Role**이 필요하다. 이번 작업에서 만든 Role은 스테이징 Bucket의 `trip-uploads/*`에 Get/Put/Tag/Delete, `trip-downloads/*`에 Get/Put/Tag, Bucket 위치 조회만 허용한다. 현재 BE `S3TripAttachmentStorageClient`의 직접 업로드 URL 발급, 원본 확인·읽기, 파생 이미지 보존, 삭제와 ZIP 생성 경로를 기준으로 했다. 다른 Bucket이나 Prefix, FE Task에는 이 권한이 없다. `ATTACHMENT_S3_BUCKET`은 후속 BE Task Definition에서 이번 Bucket 이름으로 설정한다. Task Role의 DB·Queue·다른 Secret 권한은 각 실행 계약을 확인하고 별도 추가한다.

현재 BE는 `trip-downloads/` ZIP을 `status=temporary`로 저장한다. 이 Prefix와 Tag가 모두 맞는 ZIP은 1일 뒤 만료하고, Versioning의 이전 버전과 삭제 Marker도 정리한다. **`trip-uploads/` 원본이나 파생 이미지는 자동 만료하지 않는다.** Presigned URL만 발급하고 업로드하지 않은 경우와 S3 PUT 뒤 완료 요청이 실패한 경우의 정리 대상·시점은 BE의 MySQL 업로드 상태와 함께 합의해야 한다. Tag만 보고 원본 전체를 지우는 Lifecycle은 넣지 않았다. 부하 시험 데이터 정리 정책도 실제 시험 Prefix와 결과 보존 기간이 정해진 뒤 별도 적용한다.

## 비용과 적용 전 검증

Bucket 자체에는 시간당 기본료를 가정하지 않는다. 실제 비용은 저장량, Object 요청, 데이터 전송, 이전 Version, 임시 ZIP 보존에 따라 달라진다. 스테이징에서는 업로드 시험의 사진 수·Byte, PUT/GET/DELETE 요청, Version별 저장량과 ZIP 생성량을 시험 조건과 함께 기록한다. FE/BE Task가 없고 S3 요청도 보내지 않았으므로 현재 작업만으로 업로드 비용·처리량을 실측했다고 적지 않는다. [Amazon S3 요금](https://aws.amazon.com/s3/pricing/)

명령은 Cloud 저장소 루트 `/Users/lee-y.ch/Desktop/yeodam/KTB4-2nd-Cloud`에서 실행한다. 로컬 `backend.hcl`은 [네트워크 문서](v2-staging-network.md)대로 준비하고 Git에 넣지 않는다.

```bash
export AWS_PROFILE=yeodam-admin
aws sts get-caller-identity
terraform fmt -check -recursive terraform/v2
terraform -chdir=terraform/v2/staging init -backend-config=backend.hcl
terraform -chdir=terraform/v2/staging validate
terraform -chdir=terraform/v2/staging plan -input=false
```

#131 당시 미적용 스테이징 State Plan은 **46개 생성, 변경 0, 삭제 0**이었다. #124 네트워크 15개, #126 ALB 15개, #128 ECS 기반 6개, 이번 Bucket 설정 8개와 BE Task Role/Policy 2개다. #135에서 MySQL과 Backup을 더한 전체 72개를 계정 `483175530259`, 서울 리전에 적용해 스테이징 사진 Bucket `yeodam-v2-staging-app-data-483175530259-ap-northeast-2`를 생성했다. FE 브라우저 PUT과 BE Presigned URL의 실제 연동은 아직 검증하지 않았다. Plan과 State 파일은 Git에 넣지 않는다.

적용 후에는 `terraform output`의 Bucket 이름과 BE Task Role ARN을 확인하고, AWS에서 Public Access Block·암호화·Versioning·CORS·Policy·Lifecycle을 대조한다. 브라우저 직접 PUT 검증은 FE와 BE의 V2 연동 뒤 수행한다. 그때 스테이징 Origin의 Preflight와 PUT 성공, 다른 Origin의 CORS 거부, BE 완료 요청 뒤 `HeadObject` 확인, 중복 PUT의 `If-None-Match` 동작을 기록한다. CORS 오류가 나면 브라우저 Origin과 실제 PUT Header 및 S3 CORS를 비교한다. `AccessDenied`면 Presigned URL을 만든 BE Task Role, Object Key Prefix와 Bucket Policy를 확인한다. `SignatureDoesNotMatch`면 URL 발급 시 서명한 Header와 실제 브라우저 Header, 만료 시각을 대조한다.
