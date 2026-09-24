# GitHub Actions OIDC와 AWS 배포 Role

Cloud CD는 장기 AWS Access Key를 GitHub에 저장하지 않는다. `production` Environment를 사용하는 Cloud 저장소의 Workflow만 GitHub OIDC Token으로 AWS 배포 Role을 임시로 맡는다.

```text
GitHub Actions production Job
  → token.actions.githubusercontent.com
  → sts:AssumeRoleWithWebIdentity
  → yeodam-v1-github-actions-deploy-role
  → EC2 상태 확인, Worker 전원 제어, SSM Run Command
```

## Trust 경계

배포 Role의 `aud`와 `sub`를 다음 값으로 고정한다.

```text
aud = sts.amazonaws.com
sub = repo:100-hours-a-week/KTB4-2nd-Cloud:environment:production
```

따라서 다른 저장소와 Environment 없는 Job은 Role을 맡을 수 없다. GitHub 저장소에서는 `production` Environment를 만들고 다음 보호 설정을 별도로 적용해야 한다.

- Required reviewer: Cloud 담당자와 대체 담당자
- Deployment branch: 보호된 `main`만 허용
- 자기 승인 허용 여부: 승인된 CD 설계 기준 적용

Environment 보호 설정은 Terraform이 아니라 GitHub Repository 설정에서 관리한다.

## 배포 Role 권한

- 서울 Region EC2 상태 조회
- 지정 Worker EC2 시작과 중지
- 지정 App/Worker EC2에 AWS 관리형 `AWS-RunShellScript` 실행
- SSM Managed Node 상태와 Run Command 결과 조회

배포 Role은 Parameter Store, S3와 Runtime Secret을 읽을 수 없다. 실제 배포 명령을 받은 EC2가 자신의 Instance Role로 Runtime Parameter와 GHCR Pull Credential을 조회한다.

## Workflow에서 사용할 값

Terraform 출력의 Role ARN은 Secret이 아니므로 GitHub Repository Variable로 등록한다.

```text
Name:  AWS_DEPLOY_ROLE_ARN
Value: terraform output -raw github_actions_deploy_role_arn
```

후속 CD Workflow는 다음 권한과 Environment를 사용한다.

```yaml
permissions:
  contents: read
  id-token: write

jobs:
  deploy:
    environment: production
```

`aws-actions/configure-aws-credentials`가 GitHub OIDC Token을 AWS STS에 전달하며, 발급된 임시 자격증명으로 SSM 명령을 실행한다.
