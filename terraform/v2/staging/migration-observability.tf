resource "aws_vpc_security_group_egress_rule" "load_generator_prometheus" {
  count                        = var.enable_load_generator ? 1 : 0
  security_group_id            = aws_security_group.load_generator[0].id
  referenced_security_group_id = aws_security_group.plg.id
  description                  = "Staging k6 to PLG Prometheus remote write only"
  ip_protocol                  = "tcp"
  from_port                    = 9090
  to_port                      = 9090
}

resource "aws_vpc_security_group_ingress_rule" "plg_prometheus_from_load_generator" {
  count                        = var.enable_load_generator ? 1 : 0
  security_group_id            = aws_security_group.plg.id
  referenced_security_group_id = aws_security_group.load_generator[0].id
  description                  = "Prometheus remote write from staging load generator only"
  ip_protocol                  = "tcp"
  from_port                    = 9090
  to_port                      = 9090
}

resource "aws_ssm_document" "migration_proxysql_metrics" {
  name          = "yeodam-v2-staging-migration-proxysql-metrics"
  document_type = "Command"
  content = jsonencode({
    schemaVersion = "2.2"
    description   = "Install read-only ProxySQL metrics on the V1 staging App"
    mainSteps = [{
      action = "aws:runShellScript"
      name   = "installProxyMetrics"
      inputs = { runCommand = [
        "export METRICS_SCRIPT_B64='${filebase64("${path.module}/../../../monitoring/v2-staging/proxysql-metrics.py")}'",
        "bash -s <<'YEODAM_PROXY_METRICS'\n${file("${path.module}/../../../monitoring/v2-staging/install-proxysql-metrics.sh")}\nYEODAM_PROXY_METRICS",
      ] }
    }]
  })
  tags = local.common_tags
}

resource "aws_ssm_document" "migration_dashboard" {
  name          = "yeodam-v2-staging-migration-dashboard"
  document_type = "Command"
  content = jsonencode({
    schemaVersion = "2.2"
    description   = "Provision the migration test dashboard on the existing staging Grafana"
    mainSteps = [{
      action = "aws:runShellScript"
      name   = "installDashboard"
      inputs = { runCommand = [
        "set -eu",
        "test -d /opt/yeodam/plg/grafana/dashboards",
        "printf '%s' '${filebase64("${path.module}/../../../monitoring/v2-staging/grafana/dashboards/migration-test.json")}' | base64 -d > /opt/yeodam/plg/grafana/dashboards/migration-test.json",
        "chmod 0644 /opt/yeodam/plg/grafana/dashboards/migration-test.json",
        "python3 -m json.tool /opt/yeodam/plg/grafana/dashboards/migration-test.json >/dev/null",
      ] }
    }]
  })
  tags = local.common_tags
}
