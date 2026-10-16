variable "enable_plg_external_alarm_actions" {
  description = "Keep false only during first installation until the heartbeat is healthy; true for normal operation"
  type        = bool
  default     = true
}

data "aws_sns_topic" "existing_operations_alarm" {
  name = "yeodam-v1-operations-alarm"
}

resource "aws_iam_role_policy" "plg_external_health" {
  name = "yeodam-v2-staging-plg-external-health"
  role = aws_iam_role.plg.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect   = "Allow"
    Action   = ["cloudwatch:PutMetricData"]
    Resource = "*"
    Condition = { StringEquals = {
      "cloudwatch:namespace" = "Yeodam/V2/Staging"
      "aws:RequestedRegion"  = var.aws_region
    } }
  }] })
}

resource "aws_ssm_document" "plg_external_health" {
  name          = "yeodam-v2-staging-plg-external-health"
  document_type = "Command"
  content = jsonencode({
    schemaVersion = "2.2"
    description   = "Install local readiness publisher for independent CloudWatch PLG availability alerting"
    mainSteps = [{
      action = "aws:runShellScript"
      name   = "installHeartbeat"
      inputs = { timeoutSeconds = "120", runCommand = [
        "set -eu",
        "export PLG_INSTANCE_ID='${aws_instance.plg.id}'",
        "export PUBLISHER_B64='${filebase64("${path.module}/../../../monitoring/v2-staging/publish-plg-health.py")}'",
        "bash -s <<'YEODAM_PLG_HEALTH'\n${file("${path.module}/../../../monitoring/v2-staging/install-plg-health.sh")}\nYEODAM_PLG_HEALTH",
      ] }
    }]
  })
  tags = local.common_tags
}

resource "aws_cloudwatch_metric_alarm" "plg_external_health" {
  alarm_name          = "yeodam-v2-staging-plg-external-health"
  alarm_description   = "PLG health is below 1 or no heartbeat arrives. Check PLG EC2, timer, network, Grafana/Loki/Prometheus."
  namespace           = "Yeodam/V2/Staging"
  metric_name         = "PLGHealthy"
  dimensions          = { InstanceId = aws_instance.plg.id }
  statistic           = "Minimum"
  period              = 60
  evaluation_periods  = 3
  datapoints_to_alarm = 3
  comparison_operator = "LessThanThreshold"
  threshold           = 1
  treat_missing_data  = "breaching"
  actions_enabled     = var.enable_plg_external_alarm_actions
  alarm_actions       = [data.aws_sns_topic.existing_operations_alarm.arn]
  ok_actions          = [data.aws_sns_topic.existing_operations_alarm.arn]
  tags                = local.common_tags
}
