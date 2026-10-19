locals {
  production_ai_manifest          = jsondecode(file("${path.module}/../../../deploy/v2/image-manifest.json")).components["ai-app"]
  production_runner_image         = "ghcr.io/100-hours-a-week/yeodam-ai-app@${local.production_ai_manifest.digest}"
  production_ai_api_key_parameter = "/yeodam/v2/production/ai/API_KEY"
}

data "aws_s3_bucket" "production_photos" {
  bucket = "yeodam-v1-app-data-483175530259-ap-northeast-2"
}

data "aws_iam_role" "shared_gpu" {
  name = "yeodam-v2-staging-ai-gpu"
}

data "aws_iam_role" "staging_release" {
  name = "yeodam-v2-staging-release-deploy"
}

resource "aws_vpc_security_group_ingress_rule" "production_redis_runner" {
  security_group_id            = aws_security_group.ai_redis.id
  referenced_security_group_id = data.aws_security_group.shared_gpu.id
  ip_protocol                  = "tcp"
  from_port                    = 6379
  to_port                      = 6379
  description                  = "Shared GPU production process runner to dedicated AI Redis"
  depends_on                   = [aws_vpc_peering_connection.shared_gpu]
}

resource "aws_vpc_security_group_egress_rule" "production_runner_redis" {
  security_group_id            = data.aws_security_group.shared_gpu.id
  referenced_security_group_id = aws_security_group.ai_redis.id
  ip_protocol                  = "tcp"
  from_port                    = 6379
  to_port                      = 6379
  description                  = "Shared GPU to production AI Redis over peering"
  depends_on                   = [aws_vpc_peering_connection.shared_gpu]
}

# Separate named policy on the existing host role; do not replace staging policies.
resource "aws_iam_role_policy" "production_runner_runtime" {
  name = "production-ai-runner-runtime"
  role = data.aws_iam_role.shared_gpu.name
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["ssm:GetParameter", "ssm:PutParameter"], Resource = "arn:aws:ssm:ap-northeast-2:${data.aws_caller_identity.current.account_id}:parameter${local.production_ai_api_key_parameter}" },
    { Effect = "Allow", Action = ["ssm:GetParameter"], Resource = [local.ai_redis_parameter_arn, "arn:aws:ssm:ap-northeast-2:${data.aws_caller_identity.current.account_id}:parameter/yeodam/v2/staging/ai/EMBEDDING_API_KEY"] },
    { Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = module.ecs_foundation.ghcr_credentials_secret_arn },
    { Effect = "Allow", Action = ["s3:GetObject"], Resource = ["${data.aws_s3_bucket.production_photos.arn}/trip-uploads/*/derived/*", "${data.aws_s3_bucket.production_photos.arn}/trip-additions/*/derived/*"] }
  ] })
}

# Protect the shared host even if an older staging CD run still requests cleanup.
resource "aws_iam_role_policy" "shared_gpu_no_auto_stop" {
  name = "shared-gpu-production-no-auto-stop"
  role = data.aws_iam_role.staging_release.name
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Deny", Action = ["ec2:StopInstances"], Resource = data.aws_instance.shared_gpu.arn
  }] })
}

resource "aws_ssm_document" "production_runner_runtime" {
  name          = "${local.name_prefix}-ai-runner-runtime"
  document_type = "Command"
  content = jsonencode({
    schemaVersion = "2.2"
    description   = "Install only the production process runner on the shared GPU"
    mainSteps = [{
      action = "aws:runShellScript"
      name   = "installProductionRunner"
      inputs = {
        timeoutSeconds = "600"
        runCommand = [
          "export PRODUCTION_RUNNER_IMAGE='${local.production_runner_image}'",
          "export PRODUCTION_RUNNER_COMPOSE_B64='${filebase64("${path.module}/../../../deploy/v2-production/ai-runner/compose.yml")}'",
          "export PRODUCTION_PHOTO_BUCKET='${data.aws_s3_bucket.production_photos.id}'",
          "export PRODUCTION_GHCR_SECRET='${module.ecs_foundation.ghcr_credentials_secret_arn}'",
          "python3 - <<'PRODUCTION_RUNNER'\n${file("${path.module}/../../../scripts/install-v2-production-ai-runner.py")}\nPRODUCTION_RUNNER",
        ]
      }
    }]
  })
  tags = local.common_tags
}

output "production_runner_runtime_ssm_document_name" { value = aws_ssm_document.production_runner_runtime.name }
output "production_ai_image" { value = local.production_runner_image }
output "production_photo_bucket_name" { value = data.aws_s3_bucket.production_photos.id }
output "production_ai_api_key_parameter_name" { value = local.production_ai_api_key_parameter }
