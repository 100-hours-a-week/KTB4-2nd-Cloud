data "aws_route53_zone" "public" {
  name         = "yeodam-2gether.com."
  private_zone = false
}

module "app_alb" {
  source = "../modules/app-alb"

  name_prefix       = "yeodam-v2-staging"
  vpc_id            = module.network.vpc_id
  public_subnet_ids = values(module.network.public_subnet_ids)
  private_app_cidr  = module.network.private_app_cidr
  domain_name       = "staging.yeodam-2gether.com"
  hosted_zone_id    = data.aws_route53_zone.public.zone_id
  tags              = local.common_tags
}
