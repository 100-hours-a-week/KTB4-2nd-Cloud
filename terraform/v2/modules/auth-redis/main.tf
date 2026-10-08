resource "aws_security_group" "redis" {
  name_prefix = "${var.name_prefix}-redis-"
  description = "Redis access from backend tasks only"
  vpc_id      = var.vpc_id

  tags = merge(var.tags, { Name = "${var.name_prefix}-redis-sg" })
}

resource "aws_vpc_security_group_ingress_rule" "backend_redis" {
  security_group_id            = aws_security_group.redis.id
  referenced_security_group_id = var.backend_security_group_id
  description                  = "Redis TLS from backend tasks"
  from_port                    = 6379
  to_port                      = 6379
  ip_protocol                  = "tcp"
}

resource "aws_elasticache_subnet_group" "redis" {
  name       = "${var.name_prefix}-redis"
  subnet_ids = [var.private_subnet_id]

  tags = var.tags
}

resource "aws_elasticache_parameter_group" "redis" {
  name   = "${var.name_prefix}-redis7"
  family = "redis7"

  parameter {
    name  = "maxmemory-policy"
    value = "noeviction"
  }

  tags = var.tags
}

ephemeral "random_password" "redis_auth" {
  length  = 40
  special = false
}

resource "aws_secretsmanager_secret" "redis_auth" {
  name        = "${var.name_prefix}/redis-auth-token"
  description = "AUTH token for the V2 staging Redis; read only by backend task execution role"

  tags = var.tags
}

resource "aws_secretsmanager_secret_version" "redis_auth" {
  secret_id                = aws_secretsmanager_secret.redis_auth.id
  secret_string_wo         = ephemeral.random_password.redis_auth.result
  secret_string_wo_version = 1
}

# Read the persisted version so a retry after a partial apply uses the same token.
ephemeral "aws_secretsmanager_secret_version" "redis_auth" {
  secret_id  = aws_secretsmanager_secret.redis_auth.id
  version_id = aws_secretsmanager_secret_version.redis_auth.version_id
}

resource "aws_elasticache_replication_group" "redis" {
  replication_group_id       = "${var.name_prefix}-redis"
  description                = "Single-AZ auth state for V2 staging backend tasks"
  engine                     = "redis"
  engine_version             = "7.1"
  node_type                  = var.node_type
  num_cache_clusters         = 1
  automatic_failover_enabled = false
  multi_az_enabled           = false
  parameter_group_name       = aws_elasticache_parameter_group.redis.name
  subnet_group_name          = aws_elasticache_subnet_group.redis.name
  security_group_ids         = [aws_security_group.redis.id]

  at_rest_encryption_enabled = true
  transit_encryption_enabled = true
  auth_token_wo              = ephemeral.aws_secretsmanager_secret_version.redis_auth.secret_string
  auth_token_wo_version      = 1
  auth_token_update_strategy = "SET"
  apply_immediately          = true

  tags = var.tags
}
