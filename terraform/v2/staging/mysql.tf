resource "aws_security_group" "backend_tasks" {
  name_prefix = "yeodam-v2-staging-backend-"
  description = "Backend task network identity for MySQL access"
  vpc_id      = module.network.vpc_id
  tags        = merge(local.common_tags, { Name = "yeodam-v2-staging-backend-sg" })
}

resource "aws_vpc_security_group_egress_rule" "backend_https" {
  security_group_id = aws_security_group.backend_tasks.id
  description       = "Outbound HTTPS for application dependencies"
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "backend_mysql" {
  security_group_id            = aws_security_group.backend_tasks.id
  referenced_security_group_id = module.mysql.security_group_id
  description                  = "MySQL on private instance"
  from_port                    = 3306
  to_port                      = 3306
  ip_protocol                  = "tcp"
}

module "mysql" {
  source = "../modules/mysql-instance"

  name_prefix               = "yeodam-v2-staging"
  vpc_id                    = module.network.vpc_id
  private_subnet_id         = module.network.private_app_subnet_id
  availability_zone         = var.app_availability_zone
  backend_security_group_id = aws_security_group.backend_tasks.id
  ami_id                    = var.mysql_ami_id
  instance_type             = "t4g.medium"
  data_volume_size_gib      = 120
  aws_region                = var.aws_region
  mysql_image               = "docker.io/library/mysql:9.7.2@sha256:e2bde46db6563855d7177adb5f0b57b9dc663f5a20927a90f4259d3312068497"
  tags                      = local.common_tags
}

resource "aws_route53_zone" "staging_private" {
  name = "staging.yeodam.internal"

  vpc {
    vpc_id = module.network.vpc_id
  }

  tags = local.common_tags
}

# Point the stable name at a DB only after data and connectivity checks pass.
resource "aws_route53_record" "mysql_active" {
  count = var.mysql_active_private_ip == null ? 0 : 1

  zone_id = aws_route53_zone.staging_private.zone_id
  name    = "mysql.staging.yeodam.internal"
  type    = "A"
  ttl     = 30
  records = [var.mysql_active_private_ip]
}
