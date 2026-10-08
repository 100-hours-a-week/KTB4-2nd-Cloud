output "primary_endpoint_address" {
  value = aws_elasticache_replication_group.redis.primary_endpoint_address
}

output "port" {
  value = aws_elasticache_replication_group.redis.port
}

output "auth_token_secret_arn" {
  value = aws_secretsmanager_secret.redis_auth.arn
}

output "replication_group_id" {
  value = aws_elasticache_replication_group.redis.id
}

output "security_group_id" {
  value = aws_security_group.redis.id
}
