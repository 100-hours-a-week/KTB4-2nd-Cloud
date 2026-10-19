locals {
  obs_path = "${path.module}/../../../monitoring/v2-production"
  service_dashboard = templatefile("${local.obs_path}/grafana/dashboards/v2-services.json.tftpl", {
    region           = "ap-northeast-2", account_id = data.aws_caller_identity.current.account_id,
    public_alb       = split("loadbalancer/", module.app_alb.alb_arn)[1], internal_alb = aws_lb.ai_internal.arn_suffix,
    frontend_tg      = split(":", module.app_alb.frontend_target_group_arn)[5], backend_tg = split(":", module.app_alb.backend_target_group_arn)[5], ai_tg = aws_lb_target_group.ai_api.arn_suffix,
    frontend_log     = module.ecs_foundation.frontend_log_group_name, backend_log = module.ecs_foundation.backend_log_group_name, ai_log = aws_cloudwatch_log_group.ai_api.name,
    frontend_log_arn = "arn:aws:logs:ap-northeast-2:${data.aws_caller_identity.current.account_id}:log-group:${module.ecs_foundation.frontend_log_group_name}", backend_log_arn = trimsuffix(module.ecs_foundation.backend_log_group_arn, ":*"), ai_log_arn = trimsuffix(aws_cloudwatch_log_group.ai_api.arn, ":*")
  })
  production_alerts = templatefile("${local.obs_path}/alerts.json.tftpl", {
    region      = "ap-northeast-2", public_alb = split("loadbalancer/", module.app_alb.alb_arn)[1], internal_alb = aws_lb.ai_internal.arn_suffix,
    frontend_tg = split(":", module.app_alb.frontend_target_group_arn)[5], backend_tg = split(":", module.app_alb.backend_target_group_arn)[5], ai_tg = aws_lb_target_group.ai_api.arn_suffix
  })
  production_redis_dashboard = templatefile("${local.obs_path}/grafana/dashboards/redis.json.tftpl", { region = "ap-northeast-2", redis_cluster = one(module.redis.cache_cluster_ids) })
  production_logs_dashboard = templatefile("${local.obs_path}/grafana/dashboards/logs.json.tftpl", {
    frontend_log_arn = "arn:aws:logs:ap-northeast-2:${data.aws_caller_identity.current.account_id}:log-group:${module.ecs_foundation.frontend_log_group_name}", backend_log_arn = trimsuffix(module.ecs_foundation.backend_log_group_arn, ":*"), ai_api_log_arn = trimsuffix(aws_cloudwatch_log_group.ai_api.arn, ":*")
  })
  production_prometheus = templatefile("${local.obs_path}/prometheus.yml", { mysql_ip = module.mysql.private_ip, kafka_ip = aws_instance.kafka.private_ip, redis_ip = aws_instance.ai_redis.private_ip, gpu_ip = data.aws_instance.shared_gpu.private_ip, plg_ip = aws_instance.plg.private_ip })
  obs_script_root       = "/usr/local/lib/yeodam-observability"
  obs_common_files      = { for name in ["install-host.sh", "ship-host-logs.py", "probe-exporter.py"] : "${local.obs_script_root}/${name}" => file("${local.obs_path}/${name}") }
  obs_core_files = merge({ for name in ["plg-compose.yml", "loki.yml"] : "/opt/yeodam/plg/${name}" => file("${local.obs_path}/${name}") }, {
    "/opt/yeodam/plg/prometheus.yml"                                  = local.production_prometheus,
    "/opt/yeodam/plg/grafana/provisioning/datasources/production.yml" = file("${local.obs_path}/grafana/provisioning/datasources/production.yml"),
    "/opt/yeodam/plg/grafana/provisioning/dashboards/production.yml"  = file("${local.obs_path}/grafana/provisioning/dashboards/production.yml")
  }, { for name in ["install-core.sh", "prepare-ai-metrics-secret.sh", "discover-backend-tasks.py", "install-backend-discovery.sh", "discover-gpu-state.py", "install-gpu-discovery.sh", "publish-plg-health.py", "install-plg-health.sh"] : "${local.obs_script_root}/${name}" => file("${local.obs_path}/${name}") })
  obs_hosts = {
    plg   = { id = aws_instance.plg.id, ip = aws_instance.plg.private_ip },
    mysql = { id = module.mysql.instance_id, ip = module.mysql.private_ip },
    redis = { id = aws_instance.ai_redis.id, ip = aws_instance.ai_redis.private_ip },
    kafka = { id = aws_instance.kafka.id, ip = aws_instance.kafka.private_ip },
    gpu   = { id = data.aws_instance.shared_gpu.id, ip = data.aws_instance.shared_gpu.private_ip }
  }
  obs_host_extra = {
    plg   = {},
    mysql = { "${local.obs_script_root}/install-mysql-validation-metrics.sh" = file("${local.obs_path}/install-mysql-validation-metrics.sh") },
    redis = { "${local.obs_script_root}/install-ai-redis-metrics.sh" = file("${local.obs_path}/install-ai-redis-metrics.sh") },
    kafka = { "${local.obs_script_root}/install-kafka-observability.sh" = file("${local.obs_path}/install-kafka-observability.sh") },
    gpu   = { "${local.obs_script_root}/install-ai-gpu-metrics.sh" = file("${local.obs_path}/install-ai-gpu-metrics.sh") }
  }
  obs_dashboard_files = merge({ for name in ["backend", "ai-jobs", "kafka-cdc", "mysql-validation", "ai-gpu", "pipeline"] : name => file("${local.obs_path}/grafana/dashboards/${name}.json") }, { services = local.service_dashboard, redis = local.production_redis_dashboard, logs = local.production_logs_dashboard })
  obs_installations = merge(
    { for name, host in local.obs_hosts : "host-${name}" => { id = host.id, entry = "${local.obs_script_root}/install-host.sh", files = merge(local.obs_common_files, local.obs_host_extra[name]), env = {
      OBS_KIND       = name, OBS_PRIVATE_IP = host.ip, MYSQL_METRICS_ADDRESS = module.mysql.private_ip, AI_REDIS_METRICS_ADDRESS = aws_instance.ai_redis.private_ip,
      GPU_PRIVATE_IP = data.aws_instance.shared_gpu.private_ip, GPU_INSTANCE_ID = data.aws_instance.shared_gpu.id,
      JMX_CONFIG_B64 = filebase64("${local.obs_path}/connect-jmx.yml"), METRICS_COMPOSE_B64 = filebase64("${local.obs_path}/kafka-observability-compose.yml"),
      SAMPLER_B64    = filebase64("${local.obs_path}/sample-ai-gpu.py"), ALLOY_B64 = filebase64("${local.obs_path}/ai-gpu.alloy"), COMPOSE_B64 = filebase64("${local.obs_path}/ai-gpu-alloy-compose.yml")
    } } },
    { core = { id = aws_instance.plg.id, entry = "${local.obs_script_root}/install-core.sh", files = local.obs_core_files, env = { GPU_INSTANCE_ID = data.aws_instance.shared_gpu.id } } },
    { alerts = { id = aws_instance.plg.id, entry = "", files = { "${local.obs_script_root}/configure-alerts.py" = file("${local.obs_path}/configure-alerts.py"), "/opt/yeodam/plg/alerts.json" = local.production_alerts }, env = {} } },
    { for name, content in local.obs_dashboard_files : "dashboard-${name}" => { id = aws_instance.plg.id, entry = "", files = { "/opt/yeodam/plg/grafana/dashboards/${name}.json" = content }, env = {} } }
  )
}
resource "aws_ssm_document" "observability_install" {
  for_each      = local.obs_installations
  name          = "${local.name_prefix}-observe-${each.key}"
  document_type = "Command"
  content       = jsonencode({ schemaVersion = "2.2", description = "Install isolated production observers: ${each.key}", mainSteps = [{ action = "aws:runShellScript", name = "installObservers", inputs = { timeoutSeconds = "900", runCommand = concat(["set -eu", "export EXPECTED_INSTANCE_ID='${each.value.id}'", "export OBS_ENTRY='${each.value.entry}'", "export OBS_ASSETS_B64='${base64encode(jsonencode(each.value.files))}'"], [for name, value in each.value.env : "export ${name}='${value}'"], ["printf '%s' '${filebase64("${local.obs_path}/install-assets.py")}' | base64 -d > /run/yeodam-observer-install.py", "python3 /run/yeodam-observer-install.py"]) } }] })
  lifecycle {
    precondition {
      condition     = length(base64encode(jsonencode(each.value.files))) < 52000
      error_message = "Split observer artifacts before exceeding the SSM document size limit."
    }
  }
}
data "aws_sns_topic" "operations_alarm" { name = "yeodam-v1-operations-alarm" }
variable "enable_production_plg_alarm_actions" {
  type        = bool
  default     = true
  description = "Enable after a healthy first heartbeat; explicit rollout setting."
}
resource "aws_cloudwatch_metric_alarm" "plg_external_health" {
  alarm_name          = "${local.plg_name}-external-health"
  alarm_description   = "Production PLG readiness missing or failed; inspect host, timers and all three containers."
  namespace           = "Yeodam/V2/Production"
  metric_name         = "PLGHealthy"
  dimensions          = { InstanceId = aws_instance.plg.id }
  statistic           = "Minimum"
  period              = 60
  evaluation_periods  = 3
  datapoints_to_alarm = 3
  comparison_operator = "LessThanThreshold"
  threshold           = 1
  treat_missing_data  = "breaching"
  actions_enabled     = var.enable_production_plg_alarm_actions
  alarm_actions       = [data.aws_sns_topic.operations_alarm.arn]
  ok_actions          = [data.aws_sns_topic.operations_alarm.arn]
}
