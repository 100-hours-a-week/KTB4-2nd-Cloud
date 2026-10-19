module "redis" {
  source = "../modules/auth-redis"

  name_prefix               = local.name_prefix
  vpc_id                    = data.aws_vpc.production.id
  private_subnet_id         = aws_subnet.private_app.id
  backend_security_group_id = aws_security_group.backend_tasks.id
  node_type                 = "cache.t4g.micro"
  tags                      = local.common_tags
}

resource "aws_vpc_security_group_egress_rule" "backend_redis" {
  security_group_id            = aws_security_group.backend_tasks.id
  referenced_security_group_id = module.redis.security_group_id
  from_port                    = 6379
  to_port                      = 6379
  ip_protocol                  = "tcp"
  description                  = "Only the production auth Redis over TLS"
}

data "aws_iam_policy_document" "backend_task_assume_role" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "backend_execution" {
  name               = "${local.name_prefix}-backend-execution"
  assume_role_policy = data.aws_iam_policy_document.backend_task_assume_role.json
  tags               = local.common_tags
}

data "aws_iam_policy_document" "backend_execution" {
  statement {
    sid       = "ReadProductionImageAndRedisCredentials"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [module.ecs_foundation.ghcr_credentials_secret_arn, module.redis.auth_token_secret_arn]
  }
  statement {
    sid       = "ReadProductionMysqlPassword"
    actions   = ["ssm:GetParameters"]
    resources = [local.mysql_app_password_arn]
  }
  statement {
    sid       = "WriteProductionBackendLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${module.ecs_foundation.backend_log_group_arn}:*"]
  }
}

resource "aws_iam_role_policy" "backend_execution" {
  name   = "${local.name_prefix}-backend-execution"
  role   = aws_iam_role.backend_execution.id
  policy = data.aws_iam_policy_document.backend_execution.json
}
