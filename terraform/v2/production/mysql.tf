locals {
  mysql_app_password_parameter = "/yeodam/v2/production/app/MYSQL_PASSWORD"
  mysql_app_password_arn       = "arn:aws:ssm:ap-northeast-2:${data.aws_caller_identity.current.account_id}:parameter${local.mysql_app_password_parameter}"
}

resource "aws_security_group" "backend_tasks" {
  name_prefix = "${local.name_prefix}-backend-"
  description = "Production Backend identity; no service is started by this configuration"
  vpc_id      = data.aws_vpc.production.id
  tags        = { Name = "${local.name_prefix}-backend-sg" }
}

resource "aws_vpc_security_group_egress_rule" "backend_https" {
  security_group_id = aws_security_group.backend_tasks.id
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"
  description       = "HTTPS dependencies through the production NAT"
}

resource "aws_vpc_security_group_egress_rule" "backend_mysql" {
  security_group_id            = aws_security_group.backend_tasks.id
  referenced_security_group_id = module.mysql.security_group_id
  from_port                    = 3306
  to_port                      = 3306
  ip_protocol                  = "tcp"
  description                  = "Only the new production MySQL"
}

module "mysql" {
  source = "../modules/mysql-instance"

  name_prefix               = local.name_prefix
  vpc_id                    = data.aws_vpc.production.id
  private_subnet_id         = aws_subnet.private_app.id
  availability_zone         = "ap-northeast-2a"
  backend_security_group_id = aws_security_group.backend_tasks.id
  ami_id                    = "ami-0ccbfe1123f2d682d"
  instance_type             = "t4g.medium"
  data_volume_size_gib      = 120
  aws_region                = "ap-northeast-2"
  mysql_image               = "docker.io/library/mysql:9.7.2@sha256:e2bde46db6563855d7177adb5f0b57b9dc663f5a20927a90f4259d3312068497"
  backup_bucket_name        = aws_s3_bucket.mysql_backup.id
  backup_bucket_arn         = aws_s3_bucket.mysql_backup.arn
  initial_database          = "yeodam"
  replication_filter_schema = null
  backup_metric_namespace   = "Yeodam/V2/Production/MySQL"
  use_https_package_mirrors = true
  additional_mysql_arguments = [
    "--server-id=327",
    "--gtid-mode=ON",
    "--enforce-gtid-consistency=ON",
    "--binlog-row-image=FULL",
    "--binlog-row-metadata=FULL",
    "--binlog-transaction-compression=OFF",
    "--innodb-buffer-pool-size=1073741824",
  ]
  tags = local.common_tags

  depends_on = [aws_route.private_internet, aws_route_table_association.private_app]
}

resource "aws_route53_zone" "production_private" {
  name = "production.yeodam.internal"
  vpc {
    vpc_id = data.aws_vpc.production.id
  }
  tags = local.common_tags
}

resource "aws_route53_record" "mysql_predeploy" {
  zone_id = aws_route53_zone.production_private.zone_id
  name    = "mysql-predeploy.production.yeodam.internal"
  type    = "A"
  ttl     = 30
  records = [module.mysql.private_ip]
}

# No active Writer DNS or replication channel is created before the migration rehearsal.
resource "aws_iam_role_policy" "mysql_app_password" {
  name = "${local.name_prefix}-mysql-app-password"
  role = module.mysql.instance_role_name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["ssm:GetParameter"]
      Resource = local.mysql_app_password_arn
    }]
  })
}

resource "aws_ssm_document" "mysql_app_account" {
  name          = "${local.name_prefix}-mysql-app-account"
  document_type = "Command"
  content = jsonencode({
    schemaVersion = "2.2"
    description   = "Provision only the new production MySQL application account"
    mainSteps = [{
      action = "aws:runShellScript"
      name   = "provisionAppAccount"
      inputs = { runCommand = ["export EXPECTED_INSTANCE_ID='${module.mysql.instance_id}'", "bash -s <<'YEODAM_PRODUCTION_ACCOUNT'\n${file("${path.module}/files/provision-mysql-app-account.sh")}\nYEODAM_PRODUCTION_ACCOUNT"] }
    }]
  })
  tags = local.common_tags
}
