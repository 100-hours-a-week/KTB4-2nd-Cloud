output "vpc_id" {
  description = "VPC ID for this environment"
  value       = aws_vpc.this.id
}

output "public_subnet_ids" {
  description = "Public subnets reserved for an ALB"
  value       = { for name, subnet in aws_subnet.public : name => subnet.id }
}

output "private_app_subnet_id" {
  description = "Private subnet for FE, BE and initial shared services"
  value       = aws_subnet.private_app.id
}

output "private_app_route_table_id" {
  description = "Private route table including the NAT and S3 endpoint paths"
  value       = aws_route_table.private_app.id
}

output "nat_gateway_id" {
  description = "Single NAT gateway in the App availability zone"
  value       = aws_nat_gateway.app.id
}

output "s3_gateway_endpoint_id" {
  description = "S3 gateway endpoint for private App traffic"
  value       = aws_vpc_endpoint.s3.id
}
