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
