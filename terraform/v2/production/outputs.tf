output "vpc_id" {
  description = "Existing production VPC; still owned by the V1 state"
  value       = data.aws_vpc.production.id
}

output "public_subnet_ids" {
  value = [data.aws_subnet.v1_public.id, aws_subnet.public_secondary.id]
}

output "private_app_subnet_id" {
  value      = aws_subnet.private_app.id
  depends_on = [aws_route.private_internet, aws_route_table_association.private_app]
}

output "private_app_cidr" {
  value = aws_subnet.private_app.cidr_block
}

output "nat_gateway_id" {
  value = aws_nat_gateway.app.id
}

output "alb_dns_name" {
  description = "Preview via a Host/SNI override; production DNS still points to V1"
  value       = module.app_alb.alb_dns_name
}

output "alb_arn" {
  value = module.app_alb.alb_arn
}

output "alb_zone_id" {
  value = module.app_alb.alb_zone_id
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

output "cluster_arn" {
  value = module.ecs_foundation.cluster_arn
}

output "task_execution_role_arn" {
  value = module.ecs_foundation.task_execution_role_arn
}

output "ghcr_credentials_secret_arn" {
  description = "Secret metadata only; credential values are managed outside Terraform"
  value       = module.ecs_foundation.ghcr_credentials_secret_arn
}

output "frontend_log_group_name" {
  value = module.ecs_foundation.frontend_log_group_name
}

output "backend_log_group_name" {
  value = module.ecs_foundation.backend_log_group_name
}

output "alb_logs_bucket_name" {
  value = aws_s3_bucket.alb_logs.id
}

output "mysql_instance_id" { value = module.mysql.instance_id }
output "mysql_private_ip" { value = module.mysql.private_ip }
output "mysql_data_volume_id" { value = module.mysql.data_volume_id }
output "mysql_root_password_secret_arn" { value = module.mysql.root_password_secret_arn }
output "mysql_predeploy_host" { value = aws_route53_record.mysql_predeploy.fqdn }
output "mysql_app_password_parameter" { value = local.mysql_app_password_parameter }
output "mysql_backup_bucket_name" { value = aws_s3_bucket.mysql_backup.id }
output "backend_security_group_id" { value = aws_security_group.backend_tasks.id }
output "backend_execution_role_arn" { value = aws_iam_role.backend_execution.arn }
output "redis_endpoint" { value = module.redis.primary_endpoint_address }
output "redis_port" { value = module.redis.port }
output "redis_auth_secret_arn" { value = module.redis.auth_token_secret_arn }
output "redis_replication_group_id" { value = module.redis.replication_group_id }

output "kafka_instance_id" { value = aws_instance.kafka.id }
output "kafka_private_ip" { value = aws_instance.kafka.private_ip }
output "kafka_data_volume_id" { value = aws_ebs_volume.kafka_data.id }
output "kafka_bootstrap_servers" { value = "${aws_route53_record.kafka_private.fqdn}:9092" }
output "ai_kafka_client_security_group_id" { value = aws_security_group.ai_kafka_clients.id }
output "kafka_runtime_ssm_document_name" { value = aws_ssm_document.kafka_runtime.name }
