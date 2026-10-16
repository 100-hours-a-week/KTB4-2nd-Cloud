resource "aws_iam_role" "staging_release_deploy" {
  name                 = "yeodam-v2-staging-release-deploy"
  description          = "Promote a verified V2 staging image combination"
  assume_role_policy   = data.aws_iam_policy_document.frontend_staging_deploy_assume.json
  max_session_duration = 7200
  tags                 = local.common_tags
}

resource "aws_iam_role_policy" "staging_release_deploy" {
  name = "yeodam-v2-staging-release-deploy"
  role = aws_iam_role.staging_release_deploy.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(
      jsondecode(data.aws_iam_policy_document.frontend_staging_deploy.json).Statement,
      jsondecode(data.aws_iam_policy_document.backend_staging_deploy.json).Statement,
      jsondecode(data.aws_iam_policy_document.ai_staging_deploy.json).Statement,
      [
        {
          Sid      = "ManageOnlyStagingAwsGpuPower"
          Effect   = "Allow"
          Action   = ["ec2:StartInstances", "ec2:StopInstances"]
          Resource = aws_instance.ai_gpu.arn
        },
        {
          Sid      = "ReadGpuSsmReadiness"
          Effect   = "Allow"
          Action   = ["ssm:DescribeInstanceInformation"]
          Resource = "*"
          Condition = {
            StringEquals = { "aws:RequestedRegion" = var.aws_region }
          }
        },
        {
          Sid    = "RecordOnlyStagingDeploymentState"
          Effect = "Allow"
          Action = ["ssm:GetParameter", "ssm:PutParameter"]
          Resource = [for name in ["last-success", "active"] :
            "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/yeodam/v2/staging/deploy/${name}"
          ]
        }
      ]
    )
  })
}

output "staging_release_deploy_role_arn" { value = aws_iam_role.staging_release_deploy.arn }
