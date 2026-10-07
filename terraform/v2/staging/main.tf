locals {
  common_tags = {
    Project     = "yeodam"
    Version     = "v2"
    Environment = "staging"
    ManagedBy   = "Terraform"
  }
}

module "network" {
  source = "../modules/network"

  aws_region                      = var.aws_region
  name_prefix                     = "yeodam-v2-staging"
  vpc_cidr                        = var.vpc_cidr
  app_availability_zone           = var.app_availability_zone
  alb_secondary_availability_zone = var.alb_secondary_availability_zone
  tags                            = local.common_tags
}
