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

resource "aws_route53_zone" "staging_private" {
  name = "staging.yeodam.internal"

  vpc {
    vpc_id = module.network.vpc_id
  }

  tags = local.common_tags
}

# Preserve the password reference needed to restore the retired DB's AMI.
# Moving its address does not read, rotate or rewrite the secret value.
moved {
  from = module.mysql.aws_secretsmanager_secret.root_password
  to   = aws_secretsmanager_secret.retired_mysql_root_password
}

resource "aws_secretsmanager_secret" "retired_mysql_root_password" {
  name        = "yeodam-v2-staging/mysql-root-password"
  description = "Root password for the V2 staging MySQL instance; value is set outside Terraform"
  tags        = local.common_tags

  lifecycle {
    prevent_destroy = true
  }
}
