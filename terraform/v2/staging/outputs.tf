output "vpc_id" {
  value = module.network.vpc_id
}

output "public_subnet_ids" {
  value = module.network.public_subnet_ids
}

output "private_app_subnet_id" {
  value = module.network.private_app_subnet_id
}

output "nat_gateway_id" {
  value = module.network.nat_gateway_id
}

output "s3_gateway_endpoint_id" {
  value = module.network.s3_gateway_endpoint_id
}

output "alb_dns_name" {
  value = module.app_alb.alb_dns_name
}

output "alb_security_group_id" {
  value = module.app_alb.alb_security_group_id
}

output "frontend_target_group_arn" {
  value = module.app_alb.frontend_target_group_arn
}

output "backend_target_group_arn" {
  value = module.app_alb.backend_target_group_arn
}

output "ecs_cluster_arn" {
  value = module.ecs_foundation.cluster_arn
}

output "ecs_task_execution_role_arn" {
  value = module.ecs_foundation.task_execution_role_arn
}

output "ghcr_credentials_secret_arn" {
  value = module.ecs_foundation.ghcr_credentials_secret_arn
}

output "frontend_log_group_name" {
  value = module.ecs_foundation.frontend_log_group_name
}

output "backend_log_group_name" {
  value = module.ecs_foundation.backend_log_group_name
}

output "app_data_bucket_name" {
  value = module.app_storage.bucket_name
}

output "backend_task_role_arn" {
  value = aws_iam_role.backend_task.arn
}

output "backend_task_security_group_id" { value = aws_security_group.backend_tasks.id }
output "mysql_instance_id" { value = module.mysql.instance_id }
output "mysql_private_ip" { value = module.mysql.private_ip }
output "mysql_data_volume_id" { value = module.mysql.data_volume_id }
output "mysql_root_password_secret_arn" { value = module.mysql.root_password_secret_arn }
output "mysql_private_dns_name" { value = "mysql.staging.yeodam.internal" }
output "mysql_backup_bucket_name" { value = aws_s3_bucket.mysql_backup.id }

output "redis_primary_endpoint_address" { value = module.redis.primary_endpoint_address }
output "redis_port" { value = module.redis.port }
output "redis_auth_token_secret_arn" { value = module.redis.auth_token_secret_arn }
output "redis_replication_group_id" { value = module.redis.replication_group_id }
output "backend_task_execution_role_arn" { value = aws_iam_role.backend_execution.arn }
