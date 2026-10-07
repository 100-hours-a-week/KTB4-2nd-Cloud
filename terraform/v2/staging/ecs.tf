module "ecs_foundation" {
  source = "../modules/ecs-foundation"

  name_prefix        = "yeodam-v2-staging"
  secret_name        = "yeodam/v2/staging/ghcr-credentials"
  log_retention_days = 30
  tags               = local.common_tags
}
