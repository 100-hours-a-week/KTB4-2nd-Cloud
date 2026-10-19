module "ecs_foundation" {
  source = "../modules/ecs-foundation"

  name_prefix        = local.name_prefix
  secret_name        = "yeodam/v2/production/ghcr-credentials"
  log_retention_days = 30
  tags               = local.common_tags
}

# AI service resources live in ai-api.tf; foundation credentials are populated outside state.
