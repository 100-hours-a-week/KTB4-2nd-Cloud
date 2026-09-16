output "aws_region" {
  description = "AWS Region used by the Yeodam V1 infrastructure"
  value       = var.aws_region
}

output "resource_name_prefix" {
  description = "Prefix used for Yeodam V1 AWS resource names"
  value       = local.name_prefix
}
