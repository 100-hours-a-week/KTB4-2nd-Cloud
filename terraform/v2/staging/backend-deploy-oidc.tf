locals {
  backend_service_arn = "arn:aws:ecs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:service/yeodam-v2-staging-app/yeodam-v2-staging-backend"
}

resource "aws_iam_role" "backend_staging_deploy" {
  name                 = "yeodam-v2-staging-backend-deploy"
  description          = "Cloud GitHub Actions staging backend ECS deployment"
  assume_role_policy   = data.aws_iam_policy_document.frontend_staging_deploy_assume.json
  max_session_duration = 3600

  tags = merge(local.common_tags, { Name = "yeodam-v2-staging-backend-deploy" })
}

data "aws_iam_policy_document" "backend_staging_deploy" {
  statement {
    sid       = "ManageOnlyStagingBackendService"
    actions   = ["ecs:DescribeServices", "ecs:UpdateService"]
    resources = [local.backend_service_arn]
  }

  # Task definition registration and description require Resource = "*".
  # UpdateService is limited to the staging backend service above.
  statement {
    sid       = "RegisterBackendTaskRevision"
    actions   = ["ecs:DescribeTaskDefinition", "ecs:RegisterTaskDefinition"]
    resources = ["*"]
  }

  statement {
    sid       = "ReadBackendTargetHealth"
    actions   = ["elasticloadbalancing:DescribeTargetGroups", "elasticloadbalancing:DescribeTargetHealth"]
    resources = ["*"]

    condition {
      test     = "StringEquals"
      variable = "aws:RequestedRegion"
      values   = [var.aws_region]
    }
  }

  statement {
    sid       = "PassOnlyBackendTaskRoles"
    actions   = ["iam:PassRole"]
    resources = [aws_iam_role.backend_execution.arn, aws_iam_role.backend_task.arn]

    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "backend_staging_deploy" {
  name   = "yeodam-v2-staging-backend-deploy"
  role   = aws_iam_role.backend_staging_deploy.id
  policy = data.aws_iam_policy_document.backend_staging_deploy.json
}
