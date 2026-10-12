resource "aws_vpc_security_group_egress_rule" "mysql_replica_to_plg_prometheus" {
  security_group_id            = module.mysql.security_group_id
  referenced_security_group_id = aws_security_group.plg.id
  description                  = "MySQL replica Alloy to staging Prometheus remote write"
  ip_protocol                  = "tcp"
  from_port                    = 9090
  to_port                      = 9090
}

resource "aws_vpc_security_group_ingress_rule" "plg_prometheus_from_mysql_replica" {
  security_group_id            = aws_security_group.plg.id
  referenced_security_group_id = module.mysql.security_group_id
  description                  = "MySQL replica Alloy remote write only"
  ip_protocol                  = "tcp"
  from_port                    = 9090
  to_port                      = 9090
}

resource "aws_ssm_document" "mysql_replication_source_metrics" {
  name          = "yeodam-v2-staging-mysql-source-metrics"
  document_type = "Command"
  content = jsonencode({
    schemaVersion = "2.2"
    description   = "Install V1 source binlog position sampler and forward metrics through existing Alloy"
    mainSteps = [{
      action = "aws:runShellScript"
      name   = "installSourceMetrics"
      inputs = {
        runCommand = [
          "export METRICS_SCRIPT_B64='${filebase64("${path.module}/../../../monitoring/v2-staging/mysql-replication-metrics.sh")}'",
          "export ALLOY_CONFIG_B64='${filebase64("${path.module}/../../../monitoring/v2-staging/v1-source.alloy")}'",
          "export ALLOY_COMPOSE_B64='${filebase64("${path.module}/../../../monitoring/v2-staging/v1-source-alloy-compose.yml")}'",
          "bash -s source <<'YEODAM_METRICS_INSTALL'\n${file("${path.module}/../../../monitoring/v2-staging/install-mysql-replication-metrics.sh")}\nYEODAM_METRICS_INSTALL",
        ]
      }
    }]
  })

  tags = local.common_tags
}

resource "aws_ssm_document" "mysql_replication_replica_metrics" {
  name          = "yeodam-v2-staging-mysql-replica-metrics"
  document_type = "Command"
  content = jsonencode({
    schemaVersion = "2.2"
    description   = "Install V2 MySQL replication sampler and Alloy remote write"
    mainSteps = [{
      action = "aws:runShellScript"
      name   = "installReplicaMetrics"
      inputs = {
        runCommand = [
          "export METRICS_SCRIPT_B64='${filebase64("${path.module}/../../../monitoring/v2-staging/mysql-replication-metrics.sh")}'",
          "export ALLOY_CONFIG_B64='${filebase64("${path.module}/../../../monitoring/v2-staging/mysql-replica.alloy")}'",
          "bash -s replica <<'YEODAM_METRICS_INSTALL'\n${file("${path.module}/../../../monitoring/v2-staging/install-mysql-replication-metrics.sh")}\nYEODAM_METRICS_INSTALL",
        ]
      }
    }]
  })

  tags = local.common_tags
}
