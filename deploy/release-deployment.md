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
    ├── configure-worker-idle-shutdown.sh
    ├── deploy-release.sh
    ├── render-runtime-env.sh
    ├── rollback-release.sh
    └── worker-idle-shutdown.sh
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
7. Worker는 10분 유휴 상태를 확인하는 systemd Timer 설치 또는 갱신
8. 성공한 경우에만 `/opt/yeodam/state/{scope}`의 현재·이전 Release 기록 변경
9. 실행 중이거나 남아 있는 Container가 참조하지 않는 Docker Image 정리
10. 임시 GHCR 인증 삭제

배포 전 실패하면 기존 Container를 교체하지 않는다. Container 교체 후 검증에 실패하면 현재 Release 기록은 변경하지 않으며, 상위 Workflow가 App과 Worker Rollback을 실행해야 한다.

## Host Image 보관 정책

Image의 장기 보관 위치는 EC2 Root Volume이 아니라 GHCR이다. App/Worker Host에는 실행 중이거나 Container가 참조하는 Image만 유지하고, 성공 Release 기록을 변경한 뒤 `docker image prune --all --force`로 나머지를 정리한다. Docker는 Container가 참조하는 Image를 삭제하지 않으므로 새 Release 검증 전의 실행 Image는 보호된다.

Rollback 대상 Image가 Host에 없으면 해당 Release의 `image-versions.env`에 기록된 SHA Tag와 Digest로 GHCR에서 다시 Pull한다. 따라서 로컬 Image 정리는 Rollback 기록을 없애지 않지만, Registry와 Network를 사용할 수 없는 상황에서는 Rollback 시간이 늘어나거나 실패할 수 있다.

Image 정리는 배포의 부가적인 용량 관리 단계다. 정리에 실패하더라도 이미 검증을 마친 Release를 실패나 Rollback으로 바꾸지 않고 경고를 남긴다. 운영자는 `df -h /`와 `docker system df`로 Host 용량을 확인한다.

## Worker 유휴 자동 종료

Worker 배포가 성공하면 `yeodam-worker-idle-shutdown.timer`를 활성화한다. Timer는 부팅 2분 후부터 1분마다 Loopback의 `/health`를 확인한다. 다음 조건을 모두 만족하면 10초 후 한 번 더 확인하고 `systemctl poweroff`를 실행한다.

- `status=ok`
- `active_tasks=0`
- `queued=0`
- `idle_seconds`가 600초 이상

Health 요청 실패, 비정상 응답, 준비 미완료 또는 작업 존재 시에는 종료하지 않는다. Terraform의 `instance_initiated_shutdown_behavior = "stop"` 설정에 따라 내부 Poweroff는 Instance 삭제가 아니라 Stop으로 처리된다. 다음 여행 생성 요청이 들어오면 Backend가 EC2를 다시 시작하고 Docker의 재시작 정책에 따라 AI Container가 자동 복구된다.

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

`V1 Production Deployment` Workflow를 `main`에서 수동 실행하고 배포 사유를 입력한다.

- `reason`: 배포 목적이나 변경 내용

`production` Environment 승인 후 Workflow는 실행 시점의 최신 Cloud `main` Commit을 배포 대상으로 고정하고, GitHub OIDC로 AWS 배포 Role을 맡는다. App EC2가 실행 중이고 SSM Online인지 확인한 뒤 Worker EC2의 기존 전원 상태를 기록한다. Worker가 중지 상태라면 배포 동안만 시작한다.

각 Host에는 선택한 Cloud Commit을 `/opt/yeodam/releases/{cloud-commit-sha}`로 내려받는다. App을 먼저 배포하고 Worker를 뒤이어 배포한다. App 배포 실패 시 App의 `current`를 재적용하며, Worker 배포 실패 시 Worker의 `current`와 App의 `previous`를 적용한다. 마지막에는 성공·실패와 관계없이 Workflow가 직접 시작한 Worker를 다시 중지한다.

첫 자동 배포에는 아직 `previous`가 없다. 현재 운영 중인 Image 조합과 같은 `deploy/image-versions.env`를 포함한 Cloud Commit을 먼저 성공 배포해 App/Worker의 `current`를 만든 뒤 다음 Image 조합부터 자동 Rollback을 사용한다.
