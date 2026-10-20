# Reuse the existing environment ALB; no additional load balancer.
module "grafana_web" {
  source = "../modules/grafana-web"

  domain_name           = "grafana.yeodam-2gether.com"
  hosted_zone_id        = data.aws_route53_zone.public.zone_id
  listener_arn          = module.app_alb.https_listener_arn
  name_prefix           = local.name_prefix
  vpc_id                = aws_security_group.plg.vpc_id
  private_ip            = aws_instance.plg.private_ip
  plg_security_group_id = aws_security_group.plg.id
  alb_security_group_id = module.app_alb.alb_security_group_id
  alb_dns_name          = module.app_alb.alb_dns_name
  alb_zone_id           = module.app_alb.alb_zone_id
  tags                  = local.common_tags
}

output "grafana_url" { value = module.grafana_web.url }
