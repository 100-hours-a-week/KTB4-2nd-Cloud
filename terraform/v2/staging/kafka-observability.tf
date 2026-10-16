resource "aws_vpc_security_group_egress_rule" "plg_kafka_metrics" {
  for_each                     = toset(["9308", "9404"])
  security_group_id            = aws_security_group.plg.id
  referenced_security_group_id = aws_security_group.kafka.id
  description                  = "PLG to private Kafka observability only"
  ip_protocol                  = "tcp"
  from_port                    = tonumber(each.value)
  to_port                      = tonumber(each.value)
}

resource "aws_vpc_security_group_ingress_rule" "kafka_metrics_plg" {
  for_each                     = toset(["9308", "9404"])
  security_group_id            = aws_security_group.kafka.id
  referenced_security_group_id = aws_security_group.plg.id
  description                  = "Metrics readable only by staging PLG"
  ip_protocol                  = "tcp"
  from_port                    = tonumber(each.value)
  to_port                      = tonumber(each.value)
}

resource "aws_ssm_document" "kafka_observability" {
  name          = "yeodam-v2-staging-kafka-observability"
  document_type = "Command"
  content = jsonencode({
    schemaVersion = "2.2"
    description   = "Add read-only lag exporter and Connect JMX metrics; preserve broker and offsets"
    mainSteps = [{
      action = "aws:runShellScript"
      name   = "installMetrics"
      inputs = {
        timeoutSeconds = "600"
        runCommand = [
          "export JMX_CONFIG_B64='${filebase64("${path.module}/../../../monitoring/v2-staging/connect-jmx.yml")}'",
          "export METRICS_COMPOSE_B64='${filebase64("${path.module}/../../../monitoring/v2-staging/kafka-observability-compose.yml")}'",
          "bash -s <<'YEODAM_KAFKA_METRICS'\n${file("${path.module}/../../../monitoring/v2-staging/install-kafka-observability.sh")}\nYEODAM_KAFKA_METRICS",
        ]
      }
    }]
  })
  tags = local.common_tags
}

resource "aws_ssm_document" "kafka_dashboard" {
  name          = "yeodam-v2-staging-kafka-dashboard"
  document_type = "Command"
  content = jsonencode({
    schemaVersion = "2.2"
    description   = "Provision Kafka and CDC observability dashboard on existing Grafana"
    mainSteps = [{
      action = "aws:runShellScript"
      name   = "installDashboard"
      inputs = { runCommand = [
        "set -eu",
        "test -d /opt/yeodam/plg/grafana/dashboards",
        "printf '%s' '${filebase64("${path.module}/../../../monitoring/v2-staging/grafana/dashboards/kafka-cdc.json")}' | base64 -d > /opt/yeodam/plg/grafana/dashboards/kafka-cdc.json.next",
        "python3 -m json.tool /opt/yeodam/plg/grafana/dashboards/kafka-cdc.json.next >/dev/null",
        "chmod 0644 /opt/yeodam/plg/grafana/dashboards/kafka-cdc.json.next",
        "mv /opt/yeodam/plg/grafana/dashboards/kafka-cdc.json.next /opt/yeodam/plg/grafana/dashboards/kafka-cdc.json",
      ] }
    }]
  })
  tags = local.common_tags
}
