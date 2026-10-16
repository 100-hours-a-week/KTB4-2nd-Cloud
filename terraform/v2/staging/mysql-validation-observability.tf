resource "aws_vpc_security_group_egress_rule" "plg_mysql_validation_metrics" {
  security_group_id            = aws_security_group.plg.id
  referenced_security_group_id = aws_security_group.mysql_validation.id
  description                  = "PLG to validation MySQL exporter only"
  ip_protocol                  = "tcp"
  from_port                    = 9104
  to_port                      = 9104
}

resource "aws_vpc_security_group_ingress_rule" "mysql_validation_metrics_plg" {
  security_group_id            = aws_security_group.mysql_validation.id
  referenced_security_group_id = aws_security_group.plg.id
  description                  = "Validation MySQL metrics accessible only from PLG"
  ip_protocol                  = "tcp"
  from_port                    = 9104
  to_port                      = 9104
}

resource "aws_ssm_document" "mysql_validation_metrics" {
  name          = "yeodam-v2-staging-mysql-validation-metrics"
  document_type = "Command"
  content = jsonencode({
    schemaVersion = "2.2"
    description   = "Install a status-only exporter without restarting validation MySQL"
    mainSteps = [{
      action = "aws:runShellScript"
      name   = "installMetrics"
      inputs = {
        timeoutSeconds = "300"
        runCommand = [
          "export MYSQL_METRICS_ADDRESS='${aws_instance.mysql_validation.private_ip}'",
          "bash -s <<'YEODAM_MYSQL_METRICS'\n${file("${path.module}/../../../monitoring/v2-staging/install-mysql-validation-metrics.sh")}\nYEODAM_MYSQL_METRICS",
        ]
      }
    }]
  })
  tags = local.common_tags
}

resource "aws_ssm_document" "mysql_validation_dashboard" {
  name          = "yeodam-v2-staging-mysql-validation-dashboard"
  document_type = "Command"
  content = jsonencode({
    schemaVersion = "2.2"
    description   = "Provision validation MySQL dashboard on existing Grafana"
    mainSteps = [{
      action = "aws:runShellScript"
      name   = "installDashboard"
      inputs = { runCommand = [
        "set -eu",
        "test -d /opt/yeodam/plg/grafana/dashboards",
        "printf '%s' '${filebase64("${path.module}/../../../monitoring/v2-staging/grafana/dashboards/mysql-validation.json")}' | base64 -d > /opt/yeodam/plg/grafana/dashboards/mysql-validation.json.next",
        "python3 -m json.tool /opt/yeodam/plg/grafana/dashboards/mysql-validation.json.next >/dev/null",
        "chmod 0644 /opt/yeodam/plg/grafana/dashboards/mysql-validation.json.next",
        "mv /opt/yeodam/plg/grafana/dashboards/mysql-validation.json.next /opt/yeodam/plg/grafana/dashboards/mysql-validation.json",
      ] }
    }]
  })
  tags = local.common_tags
}
