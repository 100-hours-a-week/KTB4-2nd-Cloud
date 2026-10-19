module "ecs_foundation" {
  source = "../modules/ecs-foundation"

  name_prefix        = local.name_prefix
  secret_name        = "yeodam/v2/production/ghcr-credentials"
  log_retention_days = 30
  tags               = local.common_tags
}

# Service resources live in their own files; credentials are populated outside state.
