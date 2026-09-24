# V1 Release Image Manifest 자동 갱신

FE, BE와 AI Image Workflow는 `main` Image를 GHCR에 Push한 뒤 Cloud 저장소에 `image-published` Repository Dispatch Event를 보낸다. Cloud는 Component별 Image Tag와 Digest를 검증한 뒤 `automation/release-manifest` Branch의 `deploy/image-versions.env`를 갱신한다.

열린 Release Manifest PR이 있으면 같은 PR에 변경을 누적한다. PR이 없으면 다음 제목으로 새 PR을 만든다.

```text
[chore] V1 배포 Image Manifest 갱신
```

Cloud `main`에는 자동으로 직접 Push하지 않는다. 담당자가 누적된 FE/BE/AI 조합을 확인하고 PR을 병합한 뒤 `V1 Production Deployment`를 실행한다. CD Workflow는 별도 SHA 입력 없이 실행 시점의 최신 Cloud `main` Commit을 배포한다.

## Cross-Repository Token

V1에서는 Cloud 저장소만 대상으로 하는 Fine-grained Personal Access Token을 사용한다. EC2가 GHCR Image를 Pull할 때 사용하는 PAT과 분리한다.

Token의 Repository Access는 `KTB4-2nd-Cloud`만 선택하고 다음 권한만 허용한다.

- Contents: Read and write
- Pull requests: Read and write

Token은 다음 저장소에 `CLOUD_RELEASE_TOKEN`이라는 Actions Secret으로 등록한다.

- `KTB4-2nd-FE`
- `KTB4-2nd-BE`
- `KTB4-2nd-AI`
- `KTB4-2nd-Cloud`

FE/BE/AI는 Token으로 Cloud 저장소의 Repository Dispatch API만 호출한다. Cloud Workflow는 같은 Token으로 Release Candidate Branch를 Push하고 PR을 생성한다.

## Image Workflow가 보내는 Payload

```json
{
  "event_type": "image-published",
  "client_payload": {
    "component": "backend",
    "source_repository": "100-hours-a-week/KTB4-2nd-BE",
    "source_sha": "40-character-git-commit-sha",
    "image": "ghcr.io/100-hours-a-week/yeodam-backend:sha-40-character-git-commit-sha",
    "digest": "sha256:64-character-image-digest"
  }
}
```

Cloud Workflow는 Component와 Source Repository 조합, 허용된 GHCR Package, Commit SHA Tag와 Digest 형식을 모두 확인한다. 검증에 실패하면 Manifest와 PR을 변경하지 않는다.
