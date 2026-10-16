locals {
  staging_alerts = templatefile("${path.module}/../../../monitoring/v2-staging/alerts.json.tftpl", {
    region       = var.aws_region
    public_alb   = split("loadbalancer/", module.app_alb.alb_arn)[1]
    internal_alb = aws_lb.ai_internal.arn_suffix
    frontend_tg  = split(":", module.app_alb.frontend_target_group_arn)[5]
    backend_tg   = split(":", module.app_alb.backend_target_group_arn)[5]
    ai_tg        = aws_lb_target_group.ai_api.arn_suffix
  })
}

resource "aws_iam_role_policy" "plg_discord_contact" {
  name = "yeodam-v2-staging-discord-contact"
  role = aws_iam_role.plg.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect   = "Allow"
    Action   = ["ssm:GetParameter"]
    Resource = "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/yeodam/v1/observability/discord-webhook-url"
  }] })
}

resource "aws_ssm_document" "staging_alerting" {
  name          = "yeodam-v2-staging-alerting"
  document_type = "Command"
  content = jsonencode({
    schemaVersion = "2.2"
    description   = "Provision paused V2 Grafana alerts and shared Discord contact; activation and verification are explicit"
    mainSteps = [{
      action = "aws:runShellScript"
      name   = "installAlerting"
      inputs = { timeoutSeconds = "300", runCommand = [
        "set -eu",
        "test -d /opt/yeodam/plg/grafana/provisioning",
        "printf '%s' '${filebase64("${path.module}/../../../monitoring/v2-staging/configure-alerts.py")}' | base64 -d > /usr/local/sbin/yeodam-configure-staging-alerts",
        "chmod 0755 /usr/local/sbin/yeodam-configure-staging-alerts",
        "printf '%s' '${base64encode(local.staging_alerts)}' | base64 -d > /opt/yeodam/plg/alerts.json.next",
        "python3 -m json.tool /opt/yeodam/plg/alerts.json.next >/dev/null",
        "chmod 0644 /opt/yeodam/plg/alerts.json.next",
        "mv /opt/yeodam/plg/alerts.json.next /opt/yeodam/plg/alerts.json",
        "python3 /usr/local/sbin/yeodam-configure-staging-alerts --mode install",
      ] }
    }]
  })
  tags = local.common_tags
}
