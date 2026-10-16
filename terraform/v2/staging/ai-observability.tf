resource "aws_vpc_security_group_egress_rule" "plg_ai_metrics" {
  security_group_id            = aws_security_group.plg.id
  referenced_security_group_id = aws_security_group.ai_internal_alb.id
  description                  = "PLG scrape of shared AI metrics through internal ALB"
  ip_protocol                  = "tcp"
  from_port                    = 80
  to_port                      = 80
}

resource "aws_vpc_security_group_ingress_rule" "ai_alb_plg_metrics" {
  security_group_id            = aws_security_group.ai_internal_alb.id
  referenced_security_group_id = aws_security_group.plg.id
  description                  = "PLG authenticated AI metrics requests only at network source boundary"
  ip_protocol                  = "tcp"
  from_port                    = 80
  to_port                      = 80
}

resource "aws_iam_role_policy" "plg_ai_metrics" {
  name = "yeodam-v2-staging-ai-metrics-key"
  role = aws_iam_role.plg.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect   = "Allow", Action = ["ssm:GetParameter"],
    Resource = "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/yeodam/v2/staging/ai/API_KEY"
  }] })
}

resource "aws_ssm_document" "ai_metrics" {
  name          = "yeodam-v2-staging-ai-metrics"
  document_type = "Command"
  content = jsonencode({
    schemaVersion = "2.2"
    description   = "Install authenticated shared AI metrics on existing PLG, without touching GPU/apps"
    mainSteps = [{
      action = "aws:runShellScript"
      name   = "installAIMetrics"
      inputs = { runCommand = [
        "set -eu",
        "test -d /opt/yeodam/plg/grafana/dashboards",
        "bash -s <<'YEODAM_KEY'\n${file("${path.module}/../../../monitoring/v2-staging/prepare-ai-metrics-secret.sh")}\nYEODAM_KEY",
        "tmp=$(mktemp -d /opt/yeodam/plg/ai-metrics.XXXXXX)",
        "trap 'rm -rf \"$tmp\"' EXIT",
        "printf '%s' '${filebase64("${path.module}/../../../monitoring/v2-staging/prometheus.yml")}' | base64 -d > \"$tmp/prometheus.yml\"",
        "printf '%s' '${filebase64("${path.module}/../../../monitoring/v2-staging/plg-compose.yml")}' | base64 -d > \"$tmp/plg-compose.yml\"",
        "docker run --rm --network none --entrypoint /bin/promtool -v \"$tmp/prometheus.yml:/etc/prometheus/prometheus.yml:ro\" -v /etc/yeodam/plg/ai-metrics:/run/secrets/ai-metrics:ro ${yamldecode(file("${path.module}/../../../monitoring/v2-staging/plg-compose.yml")).services.prometheus.image} check config /etc/prometheus/prometheus.yml",
        "cp \"$tmp/prometheus.yml\" /opt/yeodam/plg/prometheus.yml",
        "cp \"$tmp/plg-compose.yml\" /opt/yeodam/plg/plg-compose.yml",
        "printf '%s' '${filebase64("${path.module}/../../../monitoring/v2-staging/grafana/dashboards/ai-jobs.json")}' | base64 -d > /opt/yeodam/plg/grafana/dashboards/ai-jobs.json",
        "cd /opt/yeodam/plg",
        "docker compose -f plg-compose.yml config --quiet",
        "docker compose -f plg-compose.yml up -d --no-deps prometheus",
        "for attempt in $(seq 1 30); do curl -fsS http://127.0.0.1:9090/-/ready >/dev/null && break; sleep 2; done",
        "curl -fsS http://127.0.0.1:9090/-/ready >/dev/null",
        "docker compose -f plg-compose.yml kill -s HUP prometheus",
      ] }
    }]
  })
  tags = local.common_tags
}
