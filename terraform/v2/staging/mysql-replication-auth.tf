locals {
  mysql_replication_password_parameter = "/yeodam/v2/staging/replication/MYSQL_SOURCE_PASSWORD"
  mysql_replication_ca_parameter       = "/yeodam/v2/staging/replication/SOURCE_CA_PEM"
  mysql_replication_parameter_arns = [
    "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter${local.mysql_replication_password_parameter}",
    "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter${local.mysql_replication_ca_parameter}",
  ]
}

# The V1 rehearsal source owns its replication credential and publishes only
# its public CA certificate. Neither value is stored in Terraform state.
data "aws_iam_policy_document" "mysql_replication_source_parameters" {
  statement {
    sid       = "ManageOwnReplicationParameters"
    actions   = ["ssm:GetParameter", "ssm:PutParameter"]
    resources = local.mysql_replication_parameter_arns
  }
}

resource "aws_iam_role_policy" "mysql_replication_source_parameters" {
  name   = "yeodam-v2-staging-v1-source-mysql-replication"
  role   = aws_iam_role.v1_source_app.name
  policy = data.aws_iam_policy_document.mysql_replication_source_parameters.json
}

# The V2 MySQL host can read the same two parameters for a one-off connection
# probe and, later, its replication receiver. It cannot modify either value.
data "aws_iam_policy_document" "mysql_replication_target_parameters" {
  statement {
    sid       = "ReadSourceReplicationParameters"
    actions   = ["ssm:GetParameter"]
    resources = local.mysql_replication_parameter_arns
  }
}

resource "aws_iam_role_policy" "mysql_replication_target_parameters" {
  name   = "yeodam-v2-staging-mysql-replication-source"
  role   = module.mysql.instance_role_name
  policy = data.aws_iam_policy_document.mysql_replication_target_parameters.json
}

# Run on the V1 staging App only. The exact V2 MySQL private IP limits MySQL
# account matching in addition to the two security-group rules from #172.
resource "aws_ssm_document" "mysql_replication_source_account" {
  name          = "yeodam-v2-staging-mysql-replication-source-account"
  document_type = "Command"
  content = jsonencode({
    schemaVersion = "2.2"
    description   = "Create a TLS-only replication account on the V1 staging source"
    mainSteps = [{
      action = "aws:runShellScript"
      name   = "provisionReplicationSourceAccount"
      inputs = {
        runCommand = [
          "REPLICA_HOST='${module.mysql.private_ip}'",
          file("${path.module}/files/provision-mysql-replication-source.sh"),
        ]
      }
    }]
  })

  tags = local.common_tags
}

# Run on the V2 staging MySQL host only. The document checks verified TLS and
# a rejected plaintext login; it does not configure or start replication.
resource "aws_ssm_document" "mysql_replication_connection_probe" {
  name          = "yeodam-v2-staging-mysql-replication-connection-probe"
  document_type = "Command"
  content = jsonencode({
    schemaVersion = "2.2"
    description   = "Check V2-to-V1 replication credentials and TLS without starting replication"
    mainSteps = [{
      action = "aws:runShellScript"
      name   = "probeReplicationConnection"
      inputs = {
        runCommand = [
          "SOURCE_HOST='${aws_instance.v1_source_app.private_ip}'",
          "REPLICA_HOST='${module.mysql.private_ip}'",
          file("${path.module}/files/probe-mysql-replication-connection.sh"),
        ]
      }
    }]
  })

  tags = local.common_tags
}
