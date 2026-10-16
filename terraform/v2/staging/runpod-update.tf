resource "aws_iam_role" "runpod_update" {
  name               = "yeodam-v2-staging-runpod-update"
  description        = "Update only the staging RunPod URL and restart the AI API"
  assume_role_policy = data.aws_iam_policy_document.frontend_staging_deploy_assume.json
  tags               = local.common_tags
}

resource "aws_iam_role_policy" "runpod_update" {
  name = "staging-runpod-update"
  role = aws_iam_role.runpod_update.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["ssm:GetParameter"], Resource = [
      aws_ssm_parameter.ai_vlm_base_url.arn,
      "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/yeodam/v2/staging/ai/VLM_API_KEY"
    ] },
    { Effect = "Allow", Action = ["ssm:PutParameter"], Resource = aws_ssm_parameter.ai_vlm_base_url.arn },
    { Effect = "Allow", Action = ["ecs:DescribeServices", "ecs:UpdateService"], Resource = aws_ecs_service.ai_api.id },
    { Effect = "Allow", Action = ["ecs:DescribeTaskDefinition", "ecs:RegisterTaskDefinition"], Resource = "*" },
    { Effect = "Allow", Action = ["iam:PassRole"], Resource = [aws_iam_role.ai_api_execution.arn, aws_iam_role.ai_api_task.arn],
    Condition = { StringEquals = { "iam:PassedToService" = "ecs-tasks.amazonaws.com" } } },
    { Effect = "Allow", Action = ["elasticloadbalancing:DescribeTargetGroups", "elasticloadbalancing:DescribeTargetHealth"], Resource = "*",
    Condition = { StringEquals = { "aws:RequestedRegion" = var.aws_region } } }
  ] })
}

output "runpod_update_role_arn" { value = aws_iam_role.runpod_update.arn }
