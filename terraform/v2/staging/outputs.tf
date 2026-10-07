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
