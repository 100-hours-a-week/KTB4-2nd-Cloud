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
