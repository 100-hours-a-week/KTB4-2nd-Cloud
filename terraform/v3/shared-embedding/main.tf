# Dedicated V3 path to the existing embedding host. No instance/VPC/SG ownership transfer.
provider "aws" {
  region              = "ap-northeast-2"
  allowed_account_ids = ["483175530259"]
  default_tags { tags = { Project = "yeodam", Version = "v3", Environment = "foundation", ManagedBy = "Terraform", Issue = "569" } }
}
variable "enabled" {
  type    = bool
  default = false
}
locals {
  v3_vpc            = "vpc-0ecc3132f3b2170e9"
  gpu_vpc           = "vpc-0d84786e134e32933"
  gpu_host          = "10.30.10.162/32"
  v3_private_routes = var.enabled ? toset(["rtb-036f77e913361faa0", "rtb-0ce761884715b3a42", "rtb-04c95e08b723549a4"]) : toset([])
}
resource "aws_vpc_peering_connection" "embedding" {
  count       = var.enabled ? 1 : 0
  vpc_id      = local.v3_vpc
  peer_vpc_id = local.gpu_vpc
  auto_accept = true
  tags        = { Name = "yeodam-v3-shared-embedding" }
}
resource "aws_route" "to_embedding" {
  for_each                  = local.v3_private_routes
  route_table_id            = each.key
  destination_cidr_block    = local.gpu_host
  vpc_peering_connection_id = aws_vpc_peering_connection.embedding[0].id
}
resource "aws_route" "to_v3" {
  count                     = var.enabled ? 1 : 0
  route_table_id            = "rtb-0fc40e73348be69f0"
  destination_cidr_block    = "10.40.0.0/16"
  vpc_peering_connection_id = aws_vpc_peering_connection.embedding[0].id
}
resource "aws_vpc_security_group_ingress_rule" "embedding" {
  count                        = var.enabled ? 1 : 0
  security_group_id            = "sg-0a80da95d51c138fe"
  referenced_security_group_id = "sg-068c6d24435af33fb"
  ip_protocol                  = "tcp"
  from_port                    = 8000
  to_port                      = 8000
  description                  = "V3 app nodes to shared embedding only"
  depends_on                   = [aws_vpc_peering_connection.embedding]
}
resource "aws_route53_zone_association" "embedding" {
  count      = var.enabled ? 1 : 0
  zone_id    = "Z0848608U1XFD63QO507"
  vpc_id     = local.v3_vpc
  vpc_region = "ap-northeast-2"
}
