resource "aws_cloudwatch_log_group" "nginx_access" {
  name              = "/${var.project_name}/${var.deployment_version}/nginx/access"
  retention_in_days = 30

  tags = {
    Name = "${local.name_prefix}-nginx-access"
    Role = "observability"
  }
}

resource "aws_cloudwatch_log_group" "nginx_error" {
  name              = "/${var.project_name}/${var.deployment_version}/nginx/error"
  retention_in_days = 30

  tags = {
    Name = "${local.name_prefix}-nginx-error"
    Role = "observability"
  }
}

resource "aws_cloudwatch_log_group" "ai" {
  name              = "/${var.project_name}/${var.deployment_version}/ai"
  retention_in_days = 30

  tags = {
    Name = "${local.name_prefix}-ai"
    Role = "observability"
  }
}

resource "aws_cloudwatch_log_group" "backend" {
  name              = "/${var.project_name}/${var.deployment_version}/backend"
  retention_in_days = 30

  tags = {
    Name = "${local.name_prefix}-backend"
    Role = "observability"
  }
}

resource "aws_sns_topic" "operations_alarm" {
  name = "${local.name_prefix}-operations-alarm"

  tags = {
    Name = "${local.name_prefix}-operations-alarm"
    Role = "observability"
  }
}

resource "aws_sns_topic_subscription" "operations_alarm_email" {
  count = var.alarm_notification_email == null ? 0 : 1

  topic_arn = aws_sns_topic.operations_alarm.arn
  protocol  = "email"
  endpoint  = var.alarm_notification_email
}

resource "aws_cloudwatch_log_metric_filter" "nginx_5xx" {
  name           = "${local.name_prefix}-nginx-5xx"
  log_group_name = aws_cloudwatch_log_group.nginx_access.name
  pattern        = "{ $.status >= 500 && $.status < 600 }"

  metric_transformation {
    name          = "nginx_5xx_total"
    namespace     = "Yeodam/V1"
    value         = "1"
    default_value = 0
    unit          = "Count"
  }
}

locals {
  operations_alarm_actions = [aws_sns_topic.operations_alarm.arn]

  app_processes = {
    nginx  = "nginx"
    mysql  = "mysqld"
    docker = "dockerd"
  }

  app_filesystems = {
    root  = "/"
    mysql = "/var/lib/mysql"
  }
}

resource "aws_cloudwatch_metric_alarm" "nginx_5xx" {
  alarm_name          = "${local.name_prefix}-nginx-5xx"
  alarm_description   = "Nginx observed at least five upstream or application 5xx responses in five minutes."
  namespace           = "Yeodam/V1"
  metric_name         = "nginx_5xx_total"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  datapoints_to_alarm = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  threshold           = 5
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.operations_alarm_actions
  ok_actions          = local.operations_alarm_actions

  tags = {
    Role = "observability"
  }

  depends_on = [aws_cloudwatch_log_metric_filter.nginx_5xx]
}

# Keep the historical dashboard in AWS without retaining references that would
# require the retired App/Worker instances to exist again.
removed {
  from = aws_cloudwatch_dashboard.operations
  lifecycle {
    destroy = false
  }
}
