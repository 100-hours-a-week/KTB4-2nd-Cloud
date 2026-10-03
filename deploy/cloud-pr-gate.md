# Cloud PR Gate

Cloud PR은 `cloud-ci-gate` 하나를 병합 필수 검사로 사용한다. 문서만 바뀌어도 배포 파일의 정적 검증과 최종 검사는 실행하고, 이미지 Pull·통합 Smoke만 생략한다.

1. `validate-image-versions.sh`로 세 이미지의 SHA 태그·Digest 형식을 검사한다. 운영 App/Worker Compose를 렌더링해 FE/BE/AI 배치, `linux/amd64`, Healthcheck, 로깅과 App의 로컬 바인딩을 확인한다.
2. 코드·설정 변경 PR에서는 `GITHUB_TOKEN`으로 GHCR에 로그인한다. 태그가 매니페스트의 Digest를 가리키는지 확인하고 세 이미지를 정확한 Digest로 `linux/amd64` Pull한다. Cloud 저장소에 세 GHCR Package의 **Manage Actions access → Read** 권한이 있어야 한다. 장기 PAT나 운영 AWS 권한은 이 워크플로에 주지 않는다.
3. Pull한 이미지를 CI 전용 Compose에서 실행한다. FE는 Health를 확인하고, BE는 임시 MySQL 및 로컬 S3 에뮬레이터, AI는 `FAKE_PIPELINE=1`로 실행한다. EC2 조회는 이미 실행 중인 CI Worker만 응답하는 Stub으로 격리한다. 한 장의 PNG로 **BE→AI** 인증 → 여행 생성 → 사진 접수 → AI 처리 완료 → 여행 상세 조회를 확인한다. 실패해도 Compose Volume까지 정리한다.

이 검증은 **FE 기동과 BE/AI 이미지의 실행·API 계약**을 확인한다. 브라우저의 FE 업로드 UI, AI 실제 모델 정확도·처리시간, 운영 Nginx/EC2/S3 권한, 대용량 업로드 성능은 검증하지 않는다. AI 모델 로드·추론 검증은 AI PR Gate, 운영 부하와 관측은 별도 시험으로 다룬다.

PR 실행 결과가 성공한 다음 Cloud Ruleset의 필수 검사에 `cloud-ci-gate`를 등록한다. `packages: read`가 있어도 이미지 Pull이 403/unauthorized라면 세 Package의 Cloud 저장소 Actions access를 확인한다. Smoke 실패 시 `Image combination smoke` 작업의 서비스 로그와 응답 Status를 먼저 본다. 이 검증은 운영 배포를 시작하지 않는다.
