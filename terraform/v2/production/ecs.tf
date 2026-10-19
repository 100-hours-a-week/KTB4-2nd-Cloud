module "ecs_foundation" {
  source = "../modules/ecs-foundation"

  name_prefix        = local.name_prefix
  secret_name        = "yeodam/v2/production/ghcr-credentials"
  log_retention_days = 30
  tags               = local.common_tags
}

# No Task Definition, ECS Service or application secret value is created here.
