locals {
  outbox_cdc_password_parameter = "/yeodam/v2/production/kafka/MYSQL_CDC_PASSWORD"
  outbox_cdc_password_arn       = "arn:aws:ssm:ap-northeast-2:${data.aws_caller_identity.current.account_id}:parameter${local.outbox_cdc_password_parameter}"
}

resource "aws_vpc_security_group_egress_rule" "kafka_outbox_mysql" {
  security_group_id            = aws_security_group.kafka.id
  referenced_security_group_id = module.mysql.security_group_id
  description                  = "Outbox CDC from Kafka Connect to production MySQL"
  ip_protocol                  = "tcp"
  from_port                    = 3306
  to_port                      = 3306
}

resource "aws_vpc_security_group_ingress_rule" "mysql_outbox_cdc" {
  security_group_id            = module.mysql.security_group_id
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
  name   = "yeodam-v2-production-mysql-outbox-cdc-password"
  role   = module.mysql.instance_role_name
  policy = data.aws_iam_policy_document.mysql_outbox_cdc_password.json
}

data "aws_iam_policy_document" "kafka_outbox_cdc_password" {
  statement {
    actions   = ["ssm:GetParameter"]
    resources = [local.outbox_cdc_password_arn]
  }
}

resource "aws_iam_role_policy" "kafka_outbox_cdc_password" {
  name   = "yeodam-v2-production-kafka-outbox-cdc-password"
  role   = aws_iam_role.kafka.name
  policy = data.aws_iam_policy_document.kafka_outbox_cdc_password.json
}

resource "aws_ssm_document" "outbox_cdc_account" {
  name          = "yeodam-v2-production-outbox-cdc-account"
  document_type = "Command"
  content = jsonencode({
    schemaVersion = "2.2"
    description   = "Provision a TLS-only CDC account for the three confirmed Backend Outbox tables"
    mainSteps = [{
      action = "aws:runShellScript"
      name   = "provisionCdcAccount"
      inputs = {
        runCommand = [
          "export EXPECTED_INSTANCE_ID='${module.mysql.instance_id}'",
          "export CDC_KAFKA_HOST='${aws_instance.kafka.private_ip}'",
          "printf '%s' '${filebase64("${path.module}/../../../scripts/provision-v2-production-outbox-cdc.sh")}' | base64 --decode > /run/yeodam-outbox-cdc-account.sh",
          "bash /run/yeodam-outbox-cdc-account.sh",
        ]
      }
    }]
  })
  tags = local.common_tags
}

resource "aws_ssm_document" "outbox_connector" {
  name          = "yeodam-v2-production-outbox-connector"
  document_type = "Command"
  content = jsonencode({
    schemaVersion = "2.2"
    description   = "Connect the confirmed production Outbox tables with first-start pending-only snapshot recovery"
    mainSteps = [{
      action = "aws:runShellScript"
      name   = "configureOutboxConnector"
      inputs = {
        timeoutSeconds = "600"
        runCommand = [
          "set -eu",
          "export EXPECTED_INSTANCE_ID='${aws_instance.kafka.id}'",
          "export CDC_CONNECTOR_B64='${filebase64("${path.module}/../../../deploy/v2-production/kafka/outbox-connector.json")}'",
          "export CDC_COMPOSE_B64='${filebase64("${path.module}/../../../deploy/v2-production/kafka/compose.yml")}'",
          "printf '%s' '${filebase64("${path.module}/../../../scripts/configure-v2-production-outbox-connector.py")}' | base64 --decode > /run/yeodam-outbox-connector.py",
          "python3 /run/yeodam-outbox-connector.py",
        ]
      }
    }]
  })
  tags = local.common_tags
}
