data "aws_ssm_parameters_by_path" "production_deployment_state" {
  path            = "/yeodam/v2/production/deploy"
  recursive       = false
  with_decryption = false
}

locals {
  # This prefix contains only non-secret String deployment records. Existing
  # production releases remain pinned when the staging manifest advances.
  production_deployment_records = zipmap(data.aws_ssm_parameters_by_path.production_deployment_state.names, nonsensitive(data.aws_ssm_parameters_by_path.production_deployment_state.values))
  production_last_success_path  = "/yeodam/v2/production/deploy/last-success"
  production_release_manifest = contains(keys(local.production_deployment_records), local.production_last_success_path) ? jsondecode(
    local.production_deployment_records[local.production_last_success_path]
  ).manifest : jsondecode(file("${path.module}/../../../deploy/v2/image-manifest.json"))
  production_release_role_name = "${local.name_prefix}-release-deploy"
  production_release_subject   = "repo:100-hours-a-week@167328634/KTB4-2nd-Cloud@1344655631:environment:production"
  production_release_services  = [for name in ["frontend", "backend", "ai-api"] : "arn:aws:ecs:ap-northeast-2:${data.aws_caller_identity.current.account_id}:service/${local.name_prefix}-app/${local.name_prefix}-${name}"]
}

resource "aws_iam_role" "production_release_deploy" {
  name                 = local.production_release_role_name
  max_session_duration = 7200
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect    = "Allow", Action = "sts:AssumeRoleWithWebIdentity"
    Principal = { Federated = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:oidc-provider/token.actions.githubusercontent.com" }
    Condition = { StringEquals = {
      "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
      "token.actions.githubusercontent.com:sub" = local.production_release_subject
    } }
  }] })
  tags = local.common_tags
}

resource "aws_iam_role_policy" "production_release_deploy" {
  name = "production-release-only"
  role = aws_iam_role.production_release_deploy.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["ecs:DescribeServices", "ecs:UpdateService"], Resource = local.production_release_services },
    { Effect = "Allow", Action = ["ecs:ListTasks"], Resource = "*", Condition = { ArnEquals = { "ecs:cluster" = module.ecs_foundation.cluster_arn } } },
    { Effect = "Allow", Action = ["ecs:DescribeTasks"], Resource = "arn:aws:ecs:ap-northeast-2:${data.aws_caller_identity.current.account_id}:task/${local.name_prefix}-app/*" },
    { Effect = "Allow", Action = ["ecs:DescribeTaskDefinition", "ecs:RegisterTaskDefinition", "elasticloadbalancing:DescribeTargetHealth"], Resource = "*", Condition = { StringEquals = { "aws:RequestedRegion" = "ap-northeast-2" } } },
    { Effect = "Allow", Action = ["iam:PassRole"], Resource = [module.ecs_foundation.task_execution_role_arn, aws_iam_role.backend_execution.arn, aws_iam_role.backend_task.arn, aws_iam_role.ai_api_execution.arn, aws_iam_role.ai_api_task.arn], Condition = { StringEquals = { "iam:PassedToService" = "ecs-tasks.amazonaws.com" } } },
    { Effect = "Allow", Action = ["ssm:SendCommand"], Resource = [aws_ssm_document.production_runner_deploy.arn, data.aws_instance.shared_gpu.arn] },
    { Effect = "Allow", Action = ["ssm:GetCommandInvocation"], Resource = "*", Condition = { StringEquals = { "aws:RequestedRegion" = "ap-northeast-2" } } },
    { Effect = "Allow", Action = ["ssm:GetParameter", "ssm:PutParameter"], Resource = [for name in ["last-success", "previous-success", "active"] : "arn:aws:ssm:ap-northeast-2:${data.aws_caller_identity.current.account_id}:parameter/yeodam/v2/production/deploy/${name}"] },
    { Effect = "Deny", Action = ["ec2:StopInstances", "ec2:TerminateInstances", "route53:ChangeResourceRecordSets"], Resource = "*" }
  ] })
}

resource "aws_ssm_document" "production_runner_deploy" {
  name          = "${local.name_prefix}-ai-runner-deploy"
  document_type = "Command"
  content = jsonencode({
    schemaVersion = "2.2"
    description   = "Update only the production CPU process runner; preserve shared GPU services and configuration"
    parameters = {
      Action      = { type = "String", allowedValues = ["inspect", "deploy"] }
      ImageDigest = { type = "String", default = "", allowedPattern = "^$|^ghcr\\.io/100-hours-a-week/yeodam-ai-app@sha256:[0-9a-f]{64}$" }
    }
    mainSteps = [{
      action = "aws:runShellScript"
      name   = "productionRunnerRelease"
      inputs = {
        timeoutSeconds = "540"
        runCommand = [
          "export AI_DEPLOY_ACTION='{{ Action }}' AI_DEPLOY_IMAGE='{{ ImageDigest }}'",
          "printf '%s' '${filebase64("${path.module}/../../../scripts/deploy-v2-production-ai-runner.py")}' | base64 --decode > /run/yeodam-production-runner-deploy.py",
          "python3 /run/yeodam-production-runner-deploy.py"
        ]
      }
    }]
  })
  tags = local.common_tags
}

output "production_release_deploy_role_arn" { value = aws_iam_role.production_release_deploy.arn }
output "production_runner_deploy_document_name" { value = aws_ssm_document.production_runner_deploy.name }
