locals {
  github_oidc_provider_arn = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:oidc-provider/token.actions.githubusercontent.com"
  staging_deploy_subject   = "repo:100-hours-a-week@167328634/KTB4-2nd-Cloud@1344655631:environment:staging"
  frontend_service_arn     = "arn:aws:ecs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:service/yeodam-v2-staging-app/yeodam-v2-staging-frontend"
}

data "aws_iam_policy_document" "frontend_staging_deploy_assume" {
  statement {
    sid     = "AllowCloudStagingEnvironment"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [local.github_oidc_provider_arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = [local.staging_deploy_subject]
    }
  }
}

resource "aws_iam_role" "frontend_staging_deploy" {
  name                 = "yeodam-v2-staging-frontend-deploy"
  description          = "Cloud GitHub Actions staging frontend ECS deployment"
  assume_role_policy   = data.aws_iam_policy_document.frontend_staging_deploy_assume.json
  max_session_duration = 3600

  tags = merge(local.common_tags, { Name = "yeodam-v2-staging-frontend-deploy" })
}

data "aws_iam_policy_document" "frontend_staging_deploy" {
  statement {
    sid       = "ManageOnlyStagingFrontendService"
    actions   = ["ecs:DescribeServices", "ecs:UpdateService"]
    resources = [local.frontend_service_arn]
  }

  # ECS task definition registration and description require Resource = "*".
  # The workflow changes only the existing FE family, and UpdateService is
  # scoped to the single staging service above.
  statement {
    sid       = "RegisterFrontendTaskRevision"
    actions   = ["ecs:DescribeTaskDefinition", "ecs:RegisterTaskDefinition"]
    resources = ["*"]
  }

  statement {
    sid     = "ReadFrontendTargetHealth"
    actions = ["elasticloadbalancing:DescribeTargetHealth"]
    # DescribeTargetHealth is evaluated without a target group resource ARN.
    resources = ["*"]

    condition {
      test     = "StringEquals"
      variable = "aws:RequestedRegion"
      values   = [var.aws_region]
    }
  }

  statement {
    sid       = "PassOnlyFrontendExecutionRole"
    actions   = ["iam:PassRole"]
    resources = [module.ecs_foundation.task_execution_role_arn]

    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "frontend_staging_deploy" {
  name   = "yeodam-v2-staging-frontend-deploy"
  role   = aws_iam_role.frontend_staging_deploy.id
  policy = data.aws_iam_policy_document.frontend_staging_deploy.json
}
