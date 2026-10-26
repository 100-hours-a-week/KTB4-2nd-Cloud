# B1 uses its own MySQL process and EBS volume. Neither the A replication
# target nor the Backend validation database can be a CDC sink destination.
locals {
  mysql_cdc_test_name                 = "yeodam-v2-staging-mysql-cdc-test"
  mysql_cdc_root_parameter            = "/yeodam/v2/staging/cdc-test/ROOT_PASSWORD"
  mysql_cdc_sink_parameter            = "/yeodam/v2/staging/cdc-test/SINK_PASSWORD"
  mysql_cdc_ca_parameter              = "/yeodam/v2/staging/cdc-test/DESTINATION_CA_PEM"
  mysql_cdc_source_password_parameter = "/yeodam/v2/staging/cdc-test/SOURCE_PASSWORD"
  mysql_cdc_source_ca_parameter       = "/yeodam/v2/staging/cdc-test/SOURCE_CA_PEM"
  mysql_cdc_parameters = [
    local.mysql_cdc_root_parameter,
    local.mysql_cdc_sink_parameter,
    local.mysql_cdc_ca_parameter,
  ]
  mysql_cdc_source_parameters = [
    local.mysql_cdc_source_password_parameter,
    local.mysql_cdc_source_ca_parameter,
  ]
}

resource "aws_security_group" "mysql_cdc_test" {
  name_prefix = "${local.mysql_cdc_test_name}-"
  description = "B1 CDC destination MySQL; SSM administration"
  vpc_id      = module.network.vpc_id
  tags        = merge(local.common_tags, { Name = "${local.mysql_cdc_test_name}-sg", Role = "database-migration" })
}

resource "aws_vpc_security_group_ingress_rule" "mysql_cdc_test_connect" {
  security_group_id            = aws_security_group.mysql_cdc_test.id
  referenced_security_group_id = aws_security_group.kafka.id
  description                  = "B1 JDBC sink from private Kafka Connect host"
  ip_protocol                  = "tcp"
  from_port                    = 3306
  to_port                      = 3306
}

resource "aws_vpc_security_group_egress_rule" "kafka_mysql_cdc_test" {
  security_group_id            = aws_security_group.kafka.id
  referenced_security_group_id = aws_security_group.mysql_cdc_test.id
  description                  = "B1 JDBC sink to dedicated MySQL"
  ip_protocol                  = "tcp"
  from_port                    = 3306
  to_port                      = 3306
}

resource "aws_vpc_security_group_ingress_rule" "v1_source_mysql_cdc_test" {
  security_group_id            = aws_security_group.v1_source_app.id
  referenced_security_group_id = aws_security_group.kafka.id
  description                  = "B1 Debezium source from private Kafka Connect host"
  ip_protocol                  = "tcp"
  from_port                    = 3306
  to_port                      = 3306
}

resource "aws_vpc_security_group_egress_rule" "kafka_v1_source_mysql_cdc_test" {
  security_group_id            = aws_security_group.kafka.id
  referenced_security_group_id = aws_security_group.v1_source_app.id
  description                  = "B1 Debezium source to V1 rehearsal MySQL"
  ip_protocol                  = "tcp"
  from_port                    = 3306
  to_port                      = 3306
}

resource "aws_vpc_security_group_egress_rule" "mysql_cdc_test_https" {
  security_group_id = aws_security_group.mysql_cdc_test.id
  description       = "SSM and HTTPS bootstrap through staging NAT"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_iam_role" "mysql_cdc_test" {
  name               = local.mysql_cdc_test_name
  assume_role_policy = data.aws_iam_policy_document.v1_source_ec2_assume_role.json
  tags               = merge(local.common_tags, { Role = "database-migration" })
}

resource "aws_iam_role_policy_attachment" "mysql_cdc_test_ssm" {
  role       = aws_iam_role.mysql_cdc_test.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy" "mysql_cdc_test_parameters" {
  name = "mysql-cdc-test-own-parameters"
  role = aws_iam_role.mysql_cdc_test.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["ssm:GetParameter", "ssm:PutParameter"]
      Resource = [for name in local.mysql_cdc_parameters : "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter${name}"]
    }]
  })
}

resource "aws_iam_role_policy" "v1_source_mysql_cdc_test_parameters" {
  name = "mysql-cdc-test-source-parameters"
  role = aws_iam_role.v1_source_app.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["ssm:GetParameter", "ssm:PutParameter"]
      Resource = [for name in local.mysql_cdc_source_parameters : "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter${name}"]
    }]
  })
}

resource "aws_iam_role_policy" "kafka_mysql_cdc_test_parameters" {
  name = "mysql-cdc-test-connection-parameters"
  role = aws_iam_role.kafka.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["ssm:GetParameter"]
      Resource = [for name in concat(local.mysql_cdc_source_parameters, [local.mysql_cdc_sink_parameter, local.mysql_cdc_ca_parameter]) : "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter${name}"]
    }]
  })
}

# #331: a single-transaction, data-only dump uses the private backup bucket.
# The Source writes one immutable run prefix, B1 reads/imports and writes its
# receipt, and Kafka checks only the two receipts before starting its Sink.
data "aws_iam_policy_document" "cdc_bulk331" {
  statement {
    sid       = "SourceBulkSnapshot"
    actions   = ["s3:PutObject", "s3:GetObject", "s3:AbortMultipartUpload"]
    resources = ["${aws_s3_bucket.mysql_backup.arn}/migration/cdc-live331/*"]
  }
}

resource "aws_iam_role_policy" "v1_source_cdc_bulk331" {
  name   = "yeodam-v2-staging-cdc-bulk331-source"
  role   = aws_iam_role.v1_source_app.id
  policy = data.aws_iam_policy_document.cdc_bulk331.json
}

resource "aws_iam_role_policy" "mysql_cdc_test_bulk331" {
  name = "yeodam-v2-staging-cdc-bulk331-destination"
  role = aws_iam_role.mysql_cdc_test.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["s3:GetObject", "s3:PutObject"]
      Resource = ["${aws_s3_bucket.mysql_backup.arn}/migration/cdc-live331/*"]
    }]
  })
}

resource "aws_iam_role_policy" "kafka_cdc_bulk331_receipt" {
  name = "yeodam-v2-staging-cdc-bulk331-receipt"
  role = aws_iam_role.kafka.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["s3:GetObject"]
      Resource = ["${aws_s3_bucket.mysql_backup.arn}/migration/cdc-live331/*"]
    }]
  })
}

resource "aws_iam_instance_profile" "mysql_cdc_test" {
  name = local.mysql_cdc_test_name
  role = aws_iam_role.mysql_cdc_test.name
  tags = merge(local.common_tags, { Role = "database-migration" })
}

# The CDC test EC2 and both volumes were retired after snapshots.
# Preserve the existing DNS record in this cleanup; it is not an active DB endpoint.
resource "aws_route53_record" "mysql_cdc_test_private" {
  zone_id = aws_route53_zone.staging_private.zone_id
  name    = "mysql-cdc-test.staging.yeodam.internal"
  type    = "A"
  ttl     = 30
  records = ["10.30.10.127"]
}

resource "aws_ssm_document" "mysql_cdc_test_source_account" {
  name          = "yeodam-v2-staging-mysql-cdc-test-source-account"
  document_type = "Command"
  content = jsonencode({
    schemaVersion = "2.2"
    description   = "Provision a TLS-only B1 CDC account on the V1 rehearsal source"
    mainSteps = [{
      action = "aws:runShellScript"
      name   = "provisionCdcTestSourceAccount"
      inputs = {
        runCommand = [
          "export CDC_KAFKA_HOST='${aws_instance.kafka.private_ip}'",
          "printf '%s' '${filebase64("${path.module}/../../../scripts/provision-v2-staging-cdc-test-source.sh")}' | base64 --decode > /run/yeodam-cdc-test-source-account.sh",
          "bash /run/yeodam-cdc-test-source-account.sh",
        ]
      }
    }]
  })
  tags = local.common_tags
}

output "mysql_cdc_test_private_hostname" { value = aws_route53_record.mysql_cdc_test_private.fqdn }
output "mysql_cdc_test_root_parameter_name" { value = local.mysql_cdc_root_parameter }
output "mysql_cdc_test_sink_parameter_name" { value = local.mysql_cdc_sink_parameter }
output "mysql_cdc_test_source_parameter_name" { value = local.mysql_cdc_source_password_parameter }

resource "aws_ssm_document" "mysql_cdc_pilot304_destination" {
  name          = "yeodam-v2-staging-cdc-pilot304-destination"
  document_type = "Command"
  content = jsonencode({
    schemaVersion = "2.2"
    description   = "Create three preverified B1 pilot tables on the dedicated destination only"
    mainSteps = [{
      action = "aws:runShellScript"
      name   = "preparePilotDestination"
      inputs = {
        runCommand = [
          "export CDC_PILOT_DDL_B64='${filebase64("${path.module}/../../../deploy/v2-staging/kafka/cdc-pilot304-destination.sql")}'",
          "printf '%s' '${filebase64("${path.module}/../../../scripts/prepare-v2-staging-cdc-pilot-destination.sh")}' | base64 --decode > /run/yeodam-cdc-pilot304-destination.sh",
          "bash /run/yeodam-cdc-pilot304-destination.sh",
        ]
      }
    }]
  })
  tags = local.common_tags
}

resource "aws_ssm_document" "mysql_cdc_pilot304_connector" {
  name          = "yeodam-v2-staging-cdc-pilot304-connector"
  document_type = "Command"
  content = jsonencode({
    schemaVersion = "2.2"
    description   = "Create isolated B1 pilot topics and register one source or one FK-ordered sink"
    parameters = {
      Phase = {
        type          = "String"
        allowedValues = ["topics", "initial-source", "sink-users", "sink-trips", "sink-trip_regions", "ordered-topic", "pause-legacy-sinks", "ordered-source", "ordered-sink", "status"]
      }
    }
    mainSteps = [{
      action = "aws:runShellScript"
      name   = "configurePilotConnector"
      inputs = {
        runCommand = [
          "export CDC_PILOT_SOURCE_B64='${filebase64("${path.module}/../../../deploy/v2-staging/kafka/cdc-pilot304-source.json")}'",
          "export CDC_PILOT_SINK_B64='${filebase64("${path.module}/../../../deploy/v2-staging/kafka/cdc-pilot304-sink.json")}'",
          "printf '%s' '${filebase64("${path.module}/../../../scripts/configure-v2-staging-cdc-pilot.py")}' | base64 --decode > /run/yeodam-cdc-pilot304.py",
          "python3 /run/yeodam-cdc-pilot304.py '{{ Phase }}'",
        ]
      }
    }]
  })
  tags = local.common_tags
}
