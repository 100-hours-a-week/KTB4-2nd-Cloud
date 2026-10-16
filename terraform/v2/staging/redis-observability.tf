locals {
  redis_dashboard = templatefile("${path.module}/../../../monitoring/v2-staging/grafana/dashboards/redis.json.tftpl", {
    region        = var.aws_region
    redis_cluster = one(module.redis.cache_cluster_ids)
  })
}

resource "aws_vpc_security_group_egress_rule" "plg_ai_redis_metrics" {
  security_group_id            = aws_security_group.plg.id
  referenced_security_group_id = aws_security_group.ai_redis.id
  description                  = "PLG to AI Redis exporter only"
  ip_protocol                  = "tcp"
  from_port                    = 9121
  to_port                      = 9121
}

resource "aws_vpc_security_group_ingress_rule" "ai_redis_metrics_plg" {
  security_group_id            = aws_security_group.ai_redis.id
  referenced_security_group_id = aws_security_group.plg.id
  description                  = "AI Redis metrics accessible only from PLG"
  ip_protocol                  = "tcp"
  from_port                    = 9121
  to_port                      = 9121
}

resource "aws_ssm_document" "ai_redis_metrics" {
  name          = "yeodam-v2-staging-ai-redis-metrics"
  document_type = "Command"
  content = jsonencode({
    schemaVersion = "2.2"
    description   = "Add an observability ACL and exporter without restarting AI Redis"
    mainSteps = [{
      action = "aws:runShellScript"
      name   = "installMetrics"
      inputs = {
        timeoutSeconds = "300"
        runCommand = [
          "export AI_REDIS_METRICS_ADDRESS='${aws_instance.ai_redis.private_ip}'",
          "bash -s <<'YEODAM_REDIS_METRICS'\n${file("${path.module}/../../../monitoring/v2-staging/install-ai-redis-metrics.sh")}\nYEODAM_REDIS_METRICS",
        ]
      }
    }]
  })
  tags = local.common_tags
}

resource "aws_ssm_document" "redis_dashboard" {
  name          = "yeodam-v2-staging-redis-dashboard"
  document_type = "Command"
  content = jsonencode({
    schemaVersion = "2.2"
    description   = "Provision BE ElastiCache and AI Redis server observability"
    mainSteps = [{
      action = "aws:runShellScript"
      name   = "installDashboard"
      inputs = { runCommand = [
        "set -eu",
        "test -d /opt/yeodam/plg/grafana/dashboards",
        "printf '%s' '${base64encode(local.redis_dashboard)}' | base64 -d > /opt/yeodam/plg/grafana/dashboards/redis.json.next",
        "python3 -m json.tool /opt/yeodam/plg/grafana/dashboards/redis.json.next >/dev/null",
        "chmod 0644 /opt/yeodam/plg/grafana/dashboards/redis.json.next",
        "mv /opt/yeodam/plg/grafana/dashboards/redis.json.next /opt/yeodam/plg/grafana/dashboards/redis.json",
      ] }
    }]
  })
  tags = local.common_tags
}
