resource "aws_ssm_document" "ai_runner_deploy" {
  name          = "yeodam-v2-staging-ai-runner-deploy"
  document_type = "Command"
  content = jsonencode({
    schemaVersion = "2.2"
    description   = "Update only the staging AI process runner with an immutable image"
    parameters = {
      Action      = { type = "String", allowedValues = ["inspect", "deploy"] }
      ImageDigest = { type = "String", default = "", allowedPattern = "^$|^ghcr\\.io/100-hours-a-week/yeodam-ai-app@sha256:[0-9a-f]{64}$" }
    }
    mainSteps = [{
      action = "aws:runShellScript"
      name   = "updateRunner"
      inputs = {
        timeoutSeconds = "540"
        runCommand = [
          "export AI_DEPLOY_ACTION='{{ Action }}' AI_DEPLOY_IMAGE='{{ ImageDigest }}'",
          "printf '%s' '${filebase64("${path.module}/../../../scripts/deploy-v2-staging-ai-runner.py")}' | base64 --decode > /run/yeodam-ai-runner-deploy.py",
          "python3 /run/yeodam-ai-runner-deploy.py"
        ]
      }
    }]
  })
  tags = local.common_tags
}

resource "aws_iam_role" "ai_staging_deploy" {
  name                 = "yeodam-v2-staging-ai-deploy"
  description          = "Cloud staging AI API and process runner release deployment"
  assume_role_policy   = data.aws_iam_policy_document.frontend_staging_deploy_assume.json
  max_session_duration = 3600
  tags                 = local.common_tags
}

data "aws_iam_policy_document" "ai_staging_deploy" {
  statement {
    actions   = ["ecs:DescribeServices", "ecs:UpdateService"]
    resources = ["arn:aws:ecs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:service/yeodam-v2-staging-app/${local.ai_api_name}"]
  }
  statement {
    actions   = ["ecs:DescribeTaskDefinition", "ecs:RegisterTaskDefinition"]
    resources = ["*"]
  }
  statement {
    actions   = ["iam:PassRole"]
    resources = [aws_iam_role.ai_api_execution.arn, aws_iam_role.ai_api_task.arn]
    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["ecs-tasks.amazonaws.com"]
    }
  }
  statement {
    actions   = ["ssm:SendCommand"]
    resources = [aws_ssm_document.ai_runner_deploy.arn, aws_instance.ai_gpu.arn]
  }
  statement {
    actions   = ["ssm:GetCommandInvocation", "ec2:DescribeInstances", "elasticloadbalancing:DescribeTargetGroups", "elasticloadbalancing:DescribeTargetHealth"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "aws:RequestedRegion"
      values   = [var.aws_region]
    }
  }
}

resource "aws_iam_role_policy" "ai_staging_deploy" {
  name   = "yeodam-v2-staging-ai-deploy"
  role   = aws_iam_role.ai_staging_deploy.id
  policy = data.aws_iam_policy_document.ai_staging_deploy.json
}

output "ai_staging_deploy_role_arn" { value = aws_iam_role.ai_staging_deploy.arn }
