output "aws_region" {
  description = "AWS Region used by the Yeodam V1 infrastructure"
  value       = var.aws_region
}

output "resource_name_prefix" {
  description = "Prefix used for Yeodam V1 AWS resource names"
  value       = local.name_prefix
}

output "vpc_id" {
  description = "ID of the Yeodam V1 VPC"
  value       = aws_vpc.main.id
}

output "public_subnet_id" {
  description = "ID of the Yeodam V1 public subnet"
  value       = aws_subnet.public.id
}

output "internet_gateway_id" {
  description = "ID of the Yeodam V1 internet gateway"
  value       = aws_internet_gateway.main.id
}

output "public_route_table_id" {
  description = "ID of the Yeodam V1 public route table"
  value       = aws_route_table.public.id
}

output "s3_vpc_endpoint_id" {
  description = "ID of the Yeodam V1 S3 gateway endpoint"
  value       = aws_vpc_endpoint.s3.id
}

output "app_security_group_id" {
  description = "ID of the Yeodam V1 App security group"
  value       = aws_security_group.app.id
}

output "worker_security_group_id" {
  description = "ID of the Yeodam V1 Worker security group"
  value       = aws_security_group.worker.id
}

output "app_ec2_role_arn" {
  description = "ARN of the Yeodam V1 App EC2 IAM role"
  value       = aws_iam_role.app_ec2.arn
}

output "worker_ec2_role_arn" {
  description = "ARN of the Yeodam V1 Worker EC2 IAM role"
  value       = aws_iam_role.worker_ec2.arn
}

output "app_instance_profile_name" {
  description = "Name of the Yeodam V1 App EC2 instance profile"
  value       = aws_iam_instance_profile.app.name
}

output "worker_instance_profile_name" {
  description = "Name of the Yeodam V1 Worker EC2 instance profile"
  value       = aws_iam_instance_profile.worker.name
}

output "app_instance_id" {
  description = "ID of the Yeodam V1 App EC2 instance"
  value       = aws_instance.app.id
}

output "app_private_ip" {
  description = "Private IPv4 address of the Yeodam V1 App EC2 instance"
  value       = aws_instance.app.private_ip
}

output "app_elastic_ip" {
  description = "Elastic IPv4 address of the Yeodam V1 App EC2 instance"
  value       = aws_eip.app.public_ip
}

output "mysql_data_volume_id" {
  description = "ID of the Yeodam V1 MySQL data EBS volume"
  value       = aws_ebs_volume.mysql_data.id
}

output "mysql_data_device_name" {
  description = "Requested EC2 attachment device name for the MySQL data volume"
  value       = aws_volume_attachment.mysql_data.device_name
}

output "app_data_bucket_name" {
  description = "Name of the private Yeodam V1 application data S3 bucket"
  value       = aws_s3_bucket.app_data.id
}

output "app_data_bucket_arn" {
  description = "ARN of the private Yeodam V1 application data S3 bucket"
  value       = aws_s3_bucket.app_data.arn
}

output "worker_instance_id" {
  description = "ID of the Yeodam V1 Worker EC2 instance"
  value       = aws_instance.worker.id
}

output "worker_private_ip" {
  description = "Private IPv4 address of the Yeodam V1 Worker EC2 instance"
  value       = aws_instance.worker.private_ip
}

output "load_generator_instance_id" {
  description = "Instance ID of the temporary V1 k6 load generator, or null when disabled"
  value       = var.enable_load_generator ? aws_instance.load_generator[0].id : null
}

output "load_generator_public_ip" {
  description = "Public IPv4 address of the temporary V1 k6 load generator, or null when disabled"
  value       = var.enable_load_generator ? aws_instance.load_generator[0].public_ip : null
}

output "load_generator_fixture_s3_prefix" {
  description = "Private S3 prefix used to transfer temporary load test fixtures"
  value       = "s3://${aws_s3_bucket.app_data.id}/load-test-fixtures"
}

output "load_test_result_s3_prefix" {
  description = "Private S3 prefix used to collect k6 result files"
  value       = "s3://${aws_s3_bucket.app_data.id}/load-test-results"
}

output "github_actions_oidc_provider_arn" {
  description = "ARN of the GitHub Actions OIDC provider"
  value       = aws_iam_openid_connect_provider.github_actions.arn
}

output "github_actions_deploy_role_arn" {
  description = "ARN of the IAM role assumed by the production GitHub Actions deployment job"
  value       = aws_iam_role.github_actions_deploy.arn
}

output "github_actions_deploy_role_name" {
  description = "Name of the IAM role assumed by the production GitHub Actions deployment job"
  value       = aws_iam_role.github_actions_deploy.name
}

output "nginx_access_log_group_name" {
  description = "CloudWatch Logs group for the Yeodam V1 Nginx JSON access log"
  value       = aws_cloudwatch_log_group.nginx_access.name
}

output "nginx_error_log_group_name" {
  description = "CloudWatch Logs group for the Yeodam V1 Nginx error log"
  value       = aws_cloudwatch_log_group.nginx_error.name
}

output "ai_log_group_name" {
  description = "CloudWatch Logs group for the Yeodam V1 AI JSON container log"
  value       = aws_cloudwatch_log_group.ai.name
}

output "backend_log_group_name" {
  description = "CloudWatch Logs group for the Yeodam V1 Backend JSON container log"
  value       = aws_cloudwatch_log_group.backend.name
}

output "operations_dashboard_name" {
  description = "CloudWatch dashboard for Yeodam V1 operations"
  value       = aws_cloudwatch_dashboard.operations.dashboard_name
}

output "operations_alarm_topic_arn" {
  description = "SNS topic receiving Yeodam V1 CloudWatch alarm state changes"
  value       = aws_sns_topic.operations_alarm.arn
}

output "discord_alarm_forwarder_function_name" {
  description = "Lambda function forwarding CloudWatch alarm state changes to Discord"
  value       = aws_lambda_function.discord_alarm_forwarder.function_name
}
