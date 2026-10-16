locals {
  service_dashboard = templatefile("${path.module}/../../../monitoring/v2-staging/grafana/dashboards/v2-services.json.tftpl", {
    region           = var.aws_region
    account_id       = data.aws_caller_identity.current.account_id
    public_alb       = split("loadbalancer/", module.app_alb.alb_arn)[1]
    internal_alb     = aws_lb.ai_internal.arn_suffix
    frontend_tg      = split(":", module.app_alb.frontend_target_group_arn)[5]
    backend_tg       = split(":", module.app_alb.backend_target_group_arn)[5]
    ai_tg            = aws_lb_target_group.ai_api.arn_suffix
    frontend_log     = module.ecs_foundation.frontend_log_group_name
    backend_log      = module.ecs_foundation.backend_log_group_name
    ai_log           = aws_cloudwatch_log_group.ai_api.name
    frontend_log_arn = "arn:aws:logs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:log-group:${module.ecs_foundation.frontend_log_group_name}"
    backend_log_arn  = trimsuffix(module.ecs_foundation.backend_log_group_arn, ":*")
    ai_log_arn       = trimsuffix(aws_cloudwatch_log_group.ai_api.arn, ":*")
  })
}

resource "aws_ssm_document" "service_dashboard" {
  name          = "yeodam-v2-staging-service-dashboard"
  document_type = "Command"
  content = jsonencode({
    schemaVersion = "2.2"
    description   = "Provision V2 staging ALB/ECS metrics and service log panels without restarting applications"
    mainSteps = [{
      action = "aws:runShellScript"
      name   = "installDashboard"
      inputs = { runCommand = [
        "set -eu",
        "test -d /opt/yeodam/plg/grafana/dashboards",
        "printf '%s' '${base64encode(local.service_dashboard)}' | base64 -d > /opt/yeodam/plg/grafana/dashboards/v2-services.json.next",
        "python3 -m json.tool /opt/yeodam/plg/grafana/dashboards/v2-services.json.next >/dev/null",
        "chmod 0644 /opt/yeodam/plg/grafana/dashboards/v2-services.json.next",
        "mv /opt/yeodam/plg/grafana/dashboards/v2-services.json.next /opt/yeodam/plg/grafana/dashboards/v2-services.json",
      ] }
    }]
  })
  tags = local.common_tags
}
