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
    description   = "Provision a TLS-only CDC account for the three confirmed Backend Outbox tables"
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

resource "aws_ssm_document" "outbox_connector" {
  name          = "yeodam-v2-staging-outbox-connector"
  document_type = "Command"
  content = jsonencode({
    schemaVersion = "2.2"
    description   = "Connect the confirmed validation Outbox tables after the initial pending-row preflight"
    mainSteps = [{
      action = "aws:runShellScript"
      name   = "configureOutboxConnector"
      inputs = {
        timeoutSeconds = "600"
        runCommand = [
          "set -eu",
          "export CDC_CONNECTOR_B64='${filebase64("${path.module}/../../../deploy/v2-staging/kafka/outbox-connector.json")}'",
          "export CDC_COMPOSE_B64='${filebase64("${path.module}/../../../deploy/v2-staging/kafka/compose.yml")}'",
          "printf '%s' '${filebase64("${path.module}/../../../scripts/configure-v2-staging-outbox-connector.py")}' | base64 --decode > /run/yeodam-outbox-connector.py",
          "python3 /run/yeodam-outbox-connector.py",
        ]
      }
    }]
  })
  tags = local.common_tags
}
