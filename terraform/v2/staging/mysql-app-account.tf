locals {
  mysql_app_password_parameter = "/yeodam/v2/staging/app/MYSQL_PASSWORD"
}

# The database host reads only its application password. ECS already has
# GetParameters access to this path through its task execution role.
data "aws_iam_policy_document" "mysql_app_password" {
  statement {
    actions = ["ssm:GetParameter"]
    resources = [
      "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter${local.mysql_app_password_parameter}",
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
