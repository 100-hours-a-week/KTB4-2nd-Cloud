resource "aws_iam_role" "production_runpod_update" {
  name        = "${local.name_prefix}-runpod-update"
  description = "Update only the production RunPod URL and roll the production AI API"
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

resource "aws_iam_role_policy" "production_runpod_update" {
  name = "production-runpod-update-only"
  role = aws_iam_role.production_runpod_update.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["ssm:GetParameter"], Resource = [
      for path in ["/yeodam/v2/production/ai/VLM_BASE_URL", "/yeodam/v2/production/ai/VLM_API_KEY", "/yeodam/v2/staging/ai/VLM_BASE_URL"] :
      "arn:aws:ssm:ap-northeast-2:${data.aws_caller_identity.current.account_id}:parameter${path}"
    ] },
    { Effect = "Allow", Action = ["ssm:PutParameter"], Resource = "arn:aws:ssm:ap-northeast-2:${data.aws_caller_identity.current.account_id}:parameter/yeodam/v2/production/ai/VLM_BASE_URL" },
    { Effect = "Allow", Action = ["ecs:DescribeServices", "ecs:UpdateService"], Resource = aws_ecs_service.ai_api.id },
    { Effect = "Allow", Action = ["ecs:ListTasks"], Resource = "*", Condition = { ArnEquals = { "ecs:cluster" = module.ecs_foundation.cluster_arn } } },
    { Effect = "Allow", Action = ["ecs:DescribeTasks"], Resource = "arn:aws:ecs:ap-northeast-2:${data.aws_caller_identity.current.account_id}:task/${local.name_prefix}-app/*" },
    { Effect = "Allow", Action = ["ecs:DescribeTaskDefinition", "ecs:RegisterTaskDefinition", "elasticloadbalancing:DescribeTargetHealth"], Resource = "*", Condition = { StringEquals = { "aws:RequestedRegion" = "ap-northeast-2" } } },
    { Effect = "Allow", Action = ["iam:PassRole"], Resource = [aws_iam_role.ai_api_execution.arn, aws_iam_role.ai_api_task.arn], Condition = { StringEquals = { "iam:PassedToService" = "ecs-tasks.amazonaws.com" } } }
  ] })
}

output "production_runpod_update_role_arn" { value = aws_iam_role.production_runpod_update.arn }
