locals {
  mysql_app_password_parameter        = "/yeodam/v2/staging/app/MYSQL_PASSWORD"
  mysql_validation_password_parameter = "/yeodam/v2/staging/app/MYSQL_VALIDATION_PASSWORD"
}

# The database host reads only its application password. ECS already has
# GetParameters access to this path through its task execution role.
data "aws_iam_policy_document" "mysql_app_password" {
  statement {
    actions = ["ssm:GetParameter"]
    resources = [
      "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter${local.mysql_app_password_parameter}",
      "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter${local.mysql_validation_password_parameter}",
    ]
  }
}

resource "aws_iam_role_policy" "mysql_app_password" {
  name   = "yeodam-v2-staging-mysql-app-password"
  role   = module.mysql.instance_role_name
  policy = data.aws_iam_policy_document.mysql_app_password.json
}

# Run manually after the SecureString exists. Keeping this outside EC2 user_data
# avoids replacing the stateful MySQL instance to provision an application user.
resource "aws_ssm_document" "mysql_app_account" {
  name          = "yeodam-v2-staging-mysql-app-account"
  document_type = "Command"
  content = jsonencode({
    schemaVersion = "2.2"
    description   = "Provision the staging Backend MySQL user without exposing its password in a command"
    mainSteps = [{
      action = "aws:runShellScript"
      name   = "provisionAppAccount"
      inputs = {
        runCommand = [file("${path.module}/files/provision-mysql-app-account.sh")]
      }
    }]
  })

  tags = local.common_tags
}

resource "aws_ssm_document" "mysql_validation_account" {
  name          = "yeodam-v2-staging-mysql-validation-account"
  document_type = "Command"
  content = jsonencode({
    schemaVersion = "2.2"
    description   = "Provision the isolated staging Backend validation schema and user"
    mainSteps = [{
      action = "aws:runShellScript"
      name   = "provisionValidationAccount"
      inputs = {
        runCommand = [file("${path.module}/files/provision-mysql-validation-account.sh")]
      }
    }]
  })

  tags = local.common_tags
}

resource "aws_ssm_document" "mysql_backup_with_validation" {
  name          = "yeodam-v2-staging-mysql-backup-with-validation"
  document_type = "Command"
  content = jsonencode({
    schemaVersion = "2.2"
    description   = "Install the full backup script for both staging MySQL schemas"
    mainSteps = [{
      action = "aws:runShellScript"
      name   = "installFullBackup"
      inputs = {
        runCommand = [
          "set -eu",
          "temporary=$(mktemp /usr/local/sbin/yeodam-mysql-full-backup.XXXXXX)",
          "trap 'rm -f \"$temporary\"' EXIT",
          "printf '%s' '${filebase64("${path.module}/files/full-backup-with-validation.sh")}' | base64 -d > \"$temporary\"",
          "bash -n \"$temporary\"",
          "chmod 0750 \"$temporary\"",
          "mv \"$temporary\" /usr/local/sbin/yeodam-mysql-full-backup",
          "echo 'Staging MySQL full backup script includes both schemas.'",
        ]
      }
    }]
  })

  tags = local.common_tags
}

resource "aws_ssm_association" "mysql_backup_with_validation" {
  name             = aws_ssm_document.mysql_backup_with_validation.name
  association_name = "yeodam-v2-staging-mysql-backup-with-validation"

  targets {
    key    = "InstanceIds"
    values = [module.mysql.instance_id]
  }
}
