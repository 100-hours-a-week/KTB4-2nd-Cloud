module "redis" {
  source = "../modules/auth-redis"

  name_prefix               = "yeodam-v2-staging"
  vpc_id                    = module.network.vpc_id
  private_subnet_id         = module.network.private_app_subnet_id
  backend_security_group_id = aws_security_group.backend_tasks.id
  node_type                 = var.redis_node_type
  tags                      = local.common_tags
}

resource "aws_vpc_security_group_egress_rule" "backend_redis" {
  security_group_id            = aws_security_group.backend_tasks.id
  referenced_security_group_id = module.redis.security_group_id
  description                  = "Redis TLS for shared auth state"
  from_port                    = 6379
  to_port                      = 6379
  ip_protocol                  = "tcp"
}

resource "aws_iam_role" "backend_execution" {
  name               = "yeodam-v2-staging-backend-execution"
  assume_role_policy = data.aws_iam_policy_document.backend_task_assume_role.json

  tags = local.common_tags
}

data "aws_iam_policy_document" "backend_execution" {
  statement {
    sid       = "ReadImageCredentialsAndRedisAuth"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [module.ecs_foundation.ghcr_credentials_secret_arn, module.redis.auth_token_secret_arn]
  }

  statement {
    sid       = "WriteBackendLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${module.ecs_foundation.backend_log_group_arn}:*"]
  }
}

resource "aws_iam_role_policy" "backend_execution" {
  name   = "yeodam-v2-staging-backend-execution"
  role   = aws_iam_role.backend_execution.id
  policy = data.aws_iam_policy_document.backend_execution.json
}
