output "cluster_arn" {
  description = "App ECS cluster ARN"
  value       = aws_ecs_cluster.app.arn
}

output "task_execution_role_arn" {
  description = "Execution Role used by ECS to pull GHCR images and send logs"
  value       = aws_iam_role.task_execution.arn
}

output "ghcr_credentials_secret_arn" {
  description = "Secret ARN for Task Definition repositoryCredentials"
  value       = aws_secretsmanager_secret.ghcr_credentials.arn
}

output "frontend_log_group_name" {
  description = "FE awslogs group"
  value       = aws_cloudwatch_log_group.frontend.name
}

output "backend_log_group_name" {
  description = "BE awslogs group"
  value       = aws_cloudwatch_log_group.backend.name
}

output "backend_log_group_arn" {
  description = "BE awslogs group ARN for a dedicated backend execution role"
  value       = aws_cloudwatch_log_group.backend.arn
}
