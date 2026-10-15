locals {
  outbox_cdc_password_parameter = "/yeodam/v2/staging/kafka/MYSQL_CDC_PASSWORD"
  outbox_cdc_password_arn       = "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter${local.outbox_cdc_password_parameter}"
}

resource "aws_vpc_security_group_egress_rule" "kafka_validation_mysql" {
  security_group_id            = aws_security_group.kafka.id
  referenced_security_group_id = aws_security_group.mysql_validation.id
  description                  = "Outbox CDC from Kafka Connect to staging MySQL"
  ip_protocol                  = "tcp"
  from_port                    = 3306
  to_port                      = 3306
}

resource "aws_vpc_security_group_ingress_rule" "mysql_outbox_cdc" {
  security_group_id            = aws_security_group.mysql_validation.id
  referenced_security_group_id = aws_security_group.kafka.id
  description                  = "Outbox CDC from the private Kafka host only"
  ip_protocol                  = "tcp"
  from_port                    = 3306
  to_port                      = 3306
}

data "aws_iam_policy_document" "mysql_outbox_cdc_password" {
  statement {
    actions   = ["ssm:GetParameter", "ssm:PutParameter"]
    resources = [local.outbox_cdc_password_arn]
  }
}

resource "aws_iam_role_policy" "mysql_outbox_cdc_password" {
  name   = "yeodam-v2-staging-mysql-outbox-cdc-password"
  role   = aws_iam_role.mysql_validation.name
  policy = data.aws_iam_policy_document.mysql_outbox_cdc_password.json
}

data "aws_iam_policy_document" "kafka_outbox_cdc_password" {
  statement {
    actions   = ["ssm:GetParameter"]
    resources = [local.outbox_cdc_password_arn]
  }
}

resource "aws_iam_role_policy" "kafka_outbox_cdc_password" {
  name   = "yeodam-v2-staging-kafka-outbox-cdc-password"
  role   = aws_iam_role.kafka.name
  policy = data.aws_iam_policy_document.kafka_outbox_cdc_password.json
}

resource "aws_ssm_document" "outbox_cdc_account" {
  name          = "yeodam-v2-staging-outbox-cdc-account"
  document_type = "Command"
  content = jsonencode({
    schemaVersion = "2.2"
    description   = "Provision a TLS-only CDC account after the agreed Backend outbox table exists"
    parameters = {
      OutboxTable = {
        type              = "String"
        description       = "Confirmed Backend outbox table in yeodam_validation; no table is created here"
        allowedPattern    = "^[A-Za-z][A-Za-z0-9_]{0,63}$"
        interpolationType = "ENV_VAR"
      }
    }
    mainSteps = [{
      action = "aws:runShellScript"
      name   = "provisionCdcAccount"
      inputs = {
        runCommand = [
          "export CDC_KAFKA_HOST='${aws_instance.kafka.private_ip}'",
          "printf '%s' '${filebase64("${path.module}/../../../scripts/provision-v2-staging-outbox-cdc.sh")}' | base64 --decode > /run/yeodam-outbox-cdc-account.sh",
          "bash /run/yeodam-outbox-cdc-account.sh",
        ]
      }
    }]
  })
  tags = local.common_tags
}
