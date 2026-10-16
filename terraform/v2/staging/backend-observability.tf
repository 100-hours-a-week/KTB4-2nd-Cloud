resource "aws_vpc_security_group_egress_rule" "plg_backend_metrics" {
  security_group_id            = aws_security_group.plg.id
  referenced_security_group_id = aws_security_group.backend_tasks.id
  description                  = "PLG to backend management port only"
  ip_protocol                  = "tcp"
  from_port                    = 9091
  to_port                      = 9091
}

resource "aws_vpc_security_group_ingress_rule" "backend_metrics_plg" {
  security_group_id            = aws_security_group.backend_tasks.id
  referenced_security_group_id = aws_security_group.plg.id
  description                  = "Backend management accessible only from staging PLG"
  ip_protocol                  = "tcp"
  from_port                    = 9091
  to_port                      = 9091
}

resource "aws_iam_role_policy" "plg_backend_discovery" {
  name = "yeodam-v2-staging-backend-discovery"
  role = aws_iam_role.plg.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["ecs:ListTasks"], Resource = "*",
    Condition = { ArnEquals = { "ecs:cluster" = module.ecs_foundation.cluster_arn } } },
    { Effect = "Allow", Action = ["ecs:DescribeTasks"],
    Resource = "arn:aws:ecs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:task/yeodam-v2-staging-app/*" }
  ] })
}

resource "aws_ssm_document" "backend_discovery" {
  name          = "yeodam-v2-staging-backend-discovery"
  document_type = "Command"
  content = jsonencode({
    schemaVersion = "2.2"
    description   = "Install read-only staging ECS task discovery and backend metrics dashboard"
    mainSteps = [{
      action = "aws:runShellScript"
      name   = "installDiscovery"
      inputs = { runCommand = [
        "set -eu",
        "test -d /opt/yeodam/plg/grafana/dashboards",
        "export DISCOVERY_B64='${filebase64("${path.module}/../../../monitoring/v2-staging/discover-backend-tasks.py")}'",
        "bash -s <<'YEODAM_DISCOVERY'\n${file("${path.module}/../../../monitoring/v2-staging/install-backend-discovery.sh")}\nYEODAM_DISCOVERY",
        "printf '%s' '${filebase64("${path.module}/../../../monitoring/v2-staging/grafana/dashboards/backend.json")}' | base64 -d > /opt/yeodam/plg/grafana/dashboards/backend.json.next",
        "python3 -m json.tool /opt/yeodam/plg/grafana/dashboards/backend.json.next >/dev/null",
        "chmod 0644 /opt/yeodam/plg/grafana/dashboards/backend.json.next",
        "mv /opt/yeodam/plg/grafana/dashboards/backend.json.next /opt/yeodam/plg/grafana/dashboards/backend.json",
      ] }
    }]
  })
  tags = local.common_tags
}
