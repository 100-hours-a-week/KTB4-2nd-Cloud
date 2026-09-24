# V1 App/Worker Release 배포와 Rollback

배포 Script는 GitHub Actions가 SSM으로 각 Host에서 실행할 명령의 기준이다. OIDC와 Workflow는 별도 작업이며, 이번 Script는 `/opt/yeodam/releases/{cloud-commit-sha}`에 Release가 준비된 이후를 담당한다.

## Release 구조

```text
/opt/yeodam/releases/{cloud-commit-sha}/
├── app-compose.yml
├── worker-compose.yml
├── deploy/image-versions.env
├── nginx/yeodam.conf
└── scripts/
    ├── deploy-release.sh
    ├── render-runtime-env.sh
    └── rollback-release.sh
```

`image-versions.env`에는 통합 검증한 FE/BE/AI Image의 SHA Tag와 Digest를 기록한다. Secret은 포함하지 않는다.

## 배포

App과 Worker는 각 EC2에서 별도로 실행한다.

```bash
sudo /opt/yeodam/releases/CLOUD_SHA/scripts/deploy-release.sh \
  --scope app \
  --release-dir /opt/yeodam/releases/CLOUD_SHA

sudo /opt/yeodam/releases/CLOUD_SHA/scripts/deploy-release.sh \
  --scope worker \
  --release-dir /opt/yeodam/releases/CLOUD_SHA
```

Script는 다음 순서로 실행된다.

1. Instance Role로 Parameter Store를 조회해 `/run/yeodam/{scope}.env` 생성
2. `/yeodam/v1/deploy/*`의 GHCR 인증을 임시 Docker 설정에 주입
3. Compose 설정 확인과 Image 사전 Pull
4. Container 교체와 Health 확인
5. 실행 Image ID와 지정 Digest 일치 확인
6. App은 Nginx 설정 검증·Reload와 HTTPS Backend Health 추가 확인
7. 성공한 경우에만 `/opt/yeodam/state/{scope}`의 현재·이전 Release 기록 변경
8. 임시 GHCR 인증 삭제

배포 전 실패하면 기존 Container를 교체하지 않는다. Container 교체 후 검증에 실패하면 현재 Release 기록은 변경하지 않으며, 상위 Workflow가 App과 Worker Rollback을 실행해야 한다.

## Rollback

```bash
sudo /opt/yeodam/releases/CURRENT_SHA/scripts/rollback-release.sh --scope app
sudo /opt/yeodam/releases/CURRENT_SHA/scripts/rollback-release.sh --scope worker
```

Rollback은 기본적으로 `previous`에 기록된 Release의 Compose, Image Digest와 Nginx 설정을 다시 적용하고 같은 Health 검증을 통과한 뒤 상태를 교환한다. 배포 도중 교체된 Scope가 검증에 실패한 경우에는 아직 상태가 갱신되지 않았으므로 Workflow가 `--target current`로 직전 성공 Release를 재적용한다. DB 데이터와 Schema는 되돌리지 않는다.

첫 자동 배포 전에는 현재 수동 배포 조합을 `image-versions.env`가 포함된 Release로 준비하고 App/Worker의 `current` 상태를 초기화해야 한다. 초기화되지 않은 상태에서 첫 배포가 실패하면 자동 Rollback 대상이 없다.

## 이번 구현에서 제외한 항목

- 신규 업로드 차단과 활성 업로드 Drain: Backend 운영 Endpoint 계약 확정 후 연결
- Worker 작업 Drain: AI 종료 계약 확정 후 연결
- DB Migration 전용 명령: Flyway 실행 계약 확정 후 연결

## GitHub Actions 수동 배포

`V1 Production Deployment` Workflow를 `main`에서 수동 실행하고 다음 값을 입력한다.

- `cloud_commit`: 배포할 Cloud `main`의 전체 40자리 Commit SHA
- `reason`: 배포 목적이나 변경 내용

`production` Environment 승인 후 Workflow는 선택 Commit이 `origin/main`에 포함되는지 확인하고, GitHub OIDC로 AWS 배포 Role을 맡는다. App EC2가 실행 중이고 SSM Online인지 확인한 뒤 Worker EC2의 기존 전원 상태를 기록한다. Worker가 중지 상태라면 배포 동안만 시작한다.

각 Host에는 선택한 Cloud Commit을 `/opt/yeodam/releases/{cloud-commit-sha}`로 내려받는다. App을 먼저 배포하고 Worker를 뒤이어 배포한다. App 배포 실패 시 App의 `current`를 재적용하며, Worker 배포 실패 시 Worker의 `current`와 App의 `previous`를 적용한다. 마지막에는 성공·실패와 관계없이 Workflow가 직접 시작한 Worker를 다시 중지한다.

첫 자동 배포에는 아직 `previous`가 없다. 현재 운영 중인 Image 조합과 같은 `deploy/image-versions.env`를 포함한 Cloud Commit을 먼저 성공 배포해 App/Worker의 `current`를 만든 뒤 다음 Image 조합부터 자동 Rollback을 사용한다.
