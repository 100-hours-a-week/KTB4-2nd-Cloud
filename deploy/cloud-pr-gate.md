# Cloud PR Gate

Cloud PR은 `cloud-ci-gate` 하나를 병합 필수 검사로 사용한다. 문서만 바뀌어도 배포 파일의 정적 검증과 최종 검사는 실행하고, 이미지 Pull·통합 Smoke만 생략한다.

## 변경 범위에 따른 통합 검증

`scripts/cloud-pr-scope.py`가 PR의 merge base부터 head까지 변경된 파일을 분류한다. Checkout은 PR merge Commit을 사용하므로 실제 통합 시험은 base와 합쳐진 소스로 실행한다. 파일 이름 변경은 이전 경로의 삭제와 새 경로의 추가를 모두 분류한다.

| 변경된 입력 | V1 Smoke | V2 Smoke |
| --- | --- | --- |
| `deploy/image-versions.env`, V1 App/Worker Compose, V1 CI Compose·Stub·Seed·Smoke·실행기, V1 이미지 검증기 | 실행 | 생략 |
| `deploy/v2/`, `tests/ci/v2/`, V2 이미지 검증기와 그 단위 시험, `deploy/v2-staging/kafka/outbox-connector.json` | 생략 | 실행 |
| 위 Runtime 경로에 포함되지 않는 `.md`·`.pdf` 문서만 변경 | 생략 | 생략 |
| 양쪽 입력, Workflow·정적 검증기, 분류되지 않은 파일, 빈 Diff | 실행 | 실행 |

허용 목록은 실제 통합 시험이 읽는 입력을 기준으로 좁게 유지한다. V2라는 이름이 붙은 배포·Terraform 파일 전체를 자동으로 V2 전용으로 간주하지 않는다. 공통 의존성이 추가되면 분류와 시험도 함께 수정한다. 경로별 판정과 두 출력값은 `Release configuration`의 로그와 Actions Summary에서 확인한다. Git Diff 조회에 실패하면 정적 검증 Job과 최종 Gate가 실패한다.

정적 검증은 분류와 관계없이 실행한다. 최종 Gate는 두 판정값이 정확히 `true` 또는 `false`인지 확인하고, 필요한 Job은 `success`, 생략 대상으로 정한 Job은 `skipped`여야 통과시킨다. 필요한 Job의 실패·취소·예상 밖 생략과 정적 검증 실패는 병합을 차단한다.

두 통합 Job은 정적 검증 뒤 서로 병렬로 실행한다. 무관한 시험을 생략하면 Runner 사용은 줄일 수 있지만, 이미 다른 시험이 더 오래 걸렸다면 PR 전체 시간은 줄어들지 않을 수 있다. 이 경로 선택은 시험 자체의 시나리오·타임아웃이나 Manifest 이미지를 변경하지 않는다.

## V1 이미지 조합 검증

1. `validate-image-versions.sh`로 세 이미지의 SHA 태그·Digest 형식을 검사한다. 운영 App/Worker Compose를 렌더링해 FE/BE/AI 배치, `linux/amd64`, Healthcheck, 로깅과 App의 로컬 바인딩을 확인한다.
2. 코드·설정 변경 PR에서는 `GITHUB_TOKEN`으로 GHCR에 로그인한다. 태그가 매니페스트의 Digest를 가리키는지 확인하고 세 이미지를 정확한 Digest로 `linux/amd64` Pull한다. Cloud 저장소에 세 GHCR Package의 **Manage Actions access → Read** 권한이 있어야 한다. 장기 PAT나 운영 AWS 권한은 이 워크플로에 주지 않는다.
3. Pull한 이미지를 CI 전용 Compose에서 실행한다. FE는 Health를 확인하고, BE는 임시 MySQL 및 로컬 S3 에뮬레이터, AI는 `FAKE_PIPELINE=1`로 실행한다. EC2 조회는 이미 실행 중인 CI Worker만 응답하는 Stub으로 격리한다. 한 장의 PNG로 **BE→AI** 인증 → 여행 생성 → 사진 접수 → AI 처리 완료 → 여행 상세 조회를 확인한다. 실패해도 Compose Volume까지 정리한다.

이 검증은 **FE 기동과 BE/AI 이미지의 실행·API 계약**을 확인한다. 브라우저의 FE 업로드 UI, AI 실제 모델 정확도·처리시간, 운영 Nginx/EC2/S3 권한, 대용량 업로드 성능은 검증하지 않는다. AI 모델 로드·추론 검증은 AI PR Gate, 운영 부하와 관측은 별도 시험으로 다룬다.

PR 실행 결과가 성공한 다음 Cloud Ruleset의 필수 검사에 `cloud-ci-gate`를 등록한다. `packages: read`가 있어도 이미지 Pull이 403/unauthorized라면 세 Package의 Cloud 저장소 Actions access를 확인한다. Smoke 실패 시 `Image combination smoke` 작업의 서비스 로그와 응답 Status를 먼저 본다. 이 검증은 운영 배포를 시작하지 않는다.

## V2 이미지 조합 검증

`tests/ci/v2/run-integration.sh`가 `deploy/v2/image-manifest.json`에 고정한 FE·BE·AI 이미지를 검증하고 별도 Compose Project에서 실행한다. 임시 MySQL·인증/AI Redis·S3·Kafka·Qdrant·Connect와 모델 HTTP 대역을 사용해 Presigned 업로드, Outbox CDC, AI 처리, Kafka 결과와 BE 완료 저장을 확인한다. Connector 설정은 배포용 `deploy/v2-staging/kafka/outbox-connector.json`을 읽으므로 이 파일도 V2 Runtime 입력이다.

실패 시에도 진단 로그를 수집하고 Volume과 Project를 정리한다. `v2-image-combination-<Run ID>-<Attempt>` Artifact에는 사용한 Manifest와 결과·로그가 7일간 보존된다. 실제 GPU·RunPod 모델과 운영 AWS 권한·성능은 이 격리 시험으로 검증하지 않는다.
