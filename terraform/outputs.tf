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
