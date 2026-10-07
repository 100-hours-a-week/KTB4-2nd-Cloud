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
