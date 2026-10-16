resource "aws_vpc_security_group_egress_rule" "ai_gpu_plg_remote_write" {
  security_group_id            = aws_security_group.ai_gpu.id
  referenced_security_group_id = aws_security_group.plg.id
  description                  = "GPU metrics remote write to PLG"
  ip_protocol                  = "tcp"
  from_port                    = 9090
  to_port                      = 9090
}
resource "aws_vpc_security_group_ingress_rule" "plg_gpu_remote_write" {
  security_group_id            = aws_security_group.plg.id
  referenced_security_group_id = aws_security_group.ai_gpu.id
  description                  = "Prometheus remote write from GPU agent"
  ip_protocol                  = "tcp"
  from_port                    = 9090
  to_port                      = 9090
}
resource "aws_vpc_security_group_egress_rule" "plg_gpu_agent" {
  security_group_id            = aws_security_group.plg.id
  referenced_security_group_id = aws_security_group.ai_gpu.id
  description                  = "PLG to private GPU agent"
  ip_protocol                  = "tcp"
  from_port                    = 12345
  to_port                      = 12345
}
resource "aws_vpc_security_group_ingress_rule" "ai_gpu_agent_plg" {
  security_group_id            = aws_security_group.ai_gpu.id
  referenced_security_group_id = aws_security_group.plg.id
  description                  = "GPU agent accessible only from PLG"
  ip_protocol                  = "tcp"
  from_port                    = 12345
  to_port                      = 12345
}
resource "aws_iam_role_policy" "plg_gpu_discovery" {
  name = "yeodam-v2-staging-gpu-discovery"
  role = aws_iam_role.plg.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = ["ec2:DescribeInstances"]
      Resource  = "*"
      Condition = { StringEquals = { "aws:RequestedRegion" = var.aws_region } }
    }]
  })
}
resource "aws_ssm_document" "ai_gpu_metrics" {
  name          = "yeodam-v2-staging-ai-gpu-metrics"
  document_type = "Command"
  content = jsonencode({
    schemaVersion = "2.2"
    description   = "Install isolated GPU metrics agent without restarting application containers"
    mainSteps = [{
      action = "aws:runShellScript"
      name   = "installMetrics"
      inputs = {
        timeoutSeconds = "300"
        runCommand = [
          "set -eu",
          "export GPU_PRIVATE_IP='${aws_instance.ai_gpu.private_ip}' GPU_INSTANCE_ID='${aws_instance.ai_gpu.id}'",
          "export SAMPLER_B64='${filebase64("${path.module}/../../../monitoring/v2-staging/sample-ai-gpu.py")}'",
          "export ALLOY_B64='${filebase64("${path.module}/../../../monitoring/v2-staging/ai-gpu.alloy")}'",
          "export COMPOSE_B64='${filebase64("${path.module}/../../../monitoring/v2-staging/ai-gpu-alloy-compose.yml")}'",
          "bash -s <<'YEODAM_GPU_METRICS'\n${file("${path.module}/../../../monitoring/v2-staging/install-ai-gpu-metrics.sh")}\nYEODAM_GPU_METRICS",
        ]
      }
    }]
  })
  tags = local.common_tags
}
resource "aws_ssm_document" "gpu_discovery" {
  name          = "yeodam-v2-staging-gpu-discovery"
  document_type = "Command"
  content = jsonencode({
    schemaVersion = "2.2"
    description   = "Discover GPU lifecycle and provision AI GPU runtime dashboard"
    mainSteps = [{
      action = "aws:runShellScript"
      name   = "installDiscovery"
      inputs = { runCommand = [
        "set -eu",
        "export GPU_INSTANCE_ID='${aws_instance.ai_gpu.id}'",
        "export DISCOVERY_B64='${filebase64("${path.module}/../../../monitoring/v2-staging/discover-gpu-state.py")}'",
        "bash -s <<'YEODAM_GPU_DISCOVERY'\n${file("${path.module}/../../../monitoring/v2-staging/install-gpu-discovery.sh")}\nYEODAM_GPU_DISCOVERY",
        "printf '%s' '${filebase64("${path.module}/../../../monitoring/v2-staging/grafana/dashboards/ai-gpu.json")}' | base64 -d > /opt/yeodam/plg/grafana/dashboards/ai-gpu.json.next",
        "python3 -m json.tool /opt/yeodam/plg/grafana/dashboards/ai-gpu.json.next >/dev/null",
        "chmod 0644 /opt/yeodam/plg/grafana/dashboards/ai-gpu.json.next",
        "mv /opt/yeodam/plg/grafana/dashboards/ai-gpu.json.next /opt/yeodam/plg/grafana/dashboards/ai-gpu.json",
      ] }
    }]
  })
  tags = local.common_tags
}
