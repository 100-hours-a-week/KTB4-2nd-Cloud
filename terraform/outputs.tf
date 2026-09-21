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