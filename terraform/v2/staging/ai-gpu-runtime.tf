locals {
  ai_runtime_parameters = ["API_KEY", "EMBEDDING_API_KEY"]
}

resource "aws_iam_role_policy" "ai_gpu_runtime" {
  role = aws_iam_role.ai_gpu.id
  name = "ai-gpu-runtime"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["ssm:GetParameter", "ssm:PutParameter"]
        Resource = [for name in local.ai_runtime_parameters : "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/yeodam/v2/staging/ai/${name}"]
      },
      {
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = module.ecs_foundation.ghcr_credentials_secret_arn
      },
      {
        Effect = "Allow"
        Action = ["s3:GetObject"]
        Resource = [
          "${module.app_storage.bucket_arn}/trip-uploads/*/derived/*",
          "${module.app_storage.bucket_arn}/trip-additions/*/derived/*",
        ]
      }
    ]
  })
}

resource "aws_ssm_document" "ai_gpu_runtime" {
  name          = "yeodam-v2-staging-ai-gpu-runtime"
  document_type = "Command"
  content = jsonencode({
    schemaVersion = "2.2"
    description   = "Deploy the reviewed staging embedding, Qdrant and process runner containers"
    mainSteps = [{
      action = "aws:runShellScript"
      name   = "installAiGpuRuntime"
      inputs = {
        timeoutSeconds = "2400"
        runCommand = [
          "export AI_GPU_COMPOSE_B64='${filebase64("${path.module}/../../../deploy/v2-staging/ai-gpu/compose.yml")}'",
          "export AI_GPU_SETTINGS_B64='${filebase64("${path.module}/../../../scripts/configure-v2-staging-ai-gpu.py")}'",
          "export AI_APP_BUCKET='${module.app_storage.bucket_name}'",
          "export AI_GHCR_SECRET='${module.ecs_foundation.ghcr_credentials_secret_arn}'",
          "install -d -m 0750 /opt/yeodam-ai",
          "printf '%s' '${filebase64("${path.module}/../../../scripts/install-v2-staging-ai-gpu.sh")}' | base64 --decode > /opt/yeodam-ai/install-runtime.sh",
          "bash /opt/yeodam-ai/install-runtime.sh"
        ]
      }
    }]
  })
  tags = local.common_tags
}

output "ai_gpu_runtime_ssm_document_name" { value = aws_ssm_document.ai_gpu_runtime.name }
output "ai_embedding_base_url" { value = "http://${aws_route53_record.ai_gpu_private.fqdn}:8000/v1" }
output "ai_qdrant_url" { value = "http://${aws_route53_record.ai_gpu_private.fqdn}:6333" }
