data "aws_route53_zone" "public" {
  name         = "yeodam-2gether.com."
  private_zone = false
}

module "app_alb" {
  source = "../modules/app-alb"

  name_prefix       = local.name_prefix
  vpc_id            = data.aws_vpc.production.id
  public_subnet_ids = [data.aws_subnet.v1_public.id, aws_subnet.public_secondary.id]
  private_app_cidr  = aws_subnet.private_app.cidr_block
  domain_name       = "yeodam-2gether.com"
  hosted_zone_id    = data.aws_route53_zone.public.zone_id
  access_log_bucket = aws_s3_bucket.alb_logs.id
  access_log_prefix = "alb-access-logs"
  tags              = local.common_tags

  depends_on = [aws_s3_bucket_policy.alb_logs, aws_route.public_secondary_internet]
}

# The module creates only ACM-validation CNAMEs, not the production A/AAAA Alias.
# Target groups remain empty: no application is started or exposed by this root.
