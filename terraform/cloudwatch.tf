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

resource "aws_cloudwatch_metric_alarm" "app_status_check" {
  alarm_name          = "${local.name_prefix}-app-status-check-failed"
  alarm_description   = "App EC2 instance or system status check failed for two consecutive minutes."
  namespace           = "AWS/EC2"
  metric_name         = "StatusCheckFailed"
  dimensions          = { InstanceId = aws_instance.app.id }
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 2
  datapoints_to_alarm = 2
  comparison_operator = "GreaterThanOrEqualToThreshold"
  threshold           = 1
  treat_missing_data  = "breaching"
  alarm_actions       = local.operations_alarm_actions
  ok_actions          = local.operations_alarm_actions

  tags = {
    Role = "observability"
  }
}

resource "aws_cloudwatch_metric_alarm" "worker_status_check" {
  alarm_name          = "${local.name_prefix}-worker-status-check-failed"
  alarm_description   = "Worker EC2 status check failed while the on-demand instance is running."
  namespace           = "AWS/EC2"
  metric_name         = "StatusCheckFailed"
  dimensions          = { InstanceId = aws_instance.worker.id }
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 2
  datapoints_to_alarm = 2
  comparison_operator = "GreaterThanOrEqualToThreshold"
  threshold           = 1
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.operations_alarm_actions
  ok_actions          = local.operations_alarm_actions

  tags = {
    Role = "observability"
  }
}

resource "aws_cloudwatch_metric_alarm" "app_disk_usage" {
  for_each = local.app_filesystems

  alarm_name        = "${local.name_prefix}-app-${each.key}-disk-used-high"
  alarm_description = "App filesystem ${each.value} stayed at or above 80 percent for five minutes."
  namespace         = "CWAgent"
  metric_name       = "disk_used_percent"
  dimensions = {
    InstanceId = aws_instance.app.id
    path       = each.value
    fstype     = "ext4"
  }
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 5
  datapoints_to_alarm = 5
  comparison_operator = "GreaterThanOrEqualToThreshold"
  threshold           = 80
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.operations_alarm_actions
  ok_actions          = local.operations_alarm_actions

  tags = {
    Role = "observability"
  }
}

resource "aws_cloudwatch_metric_alarm" "app_process_missing" {
  for_each = local.app_processes

  alarm_name        = "${local.name_prefix}-app-${each.key}-process-missing"
  alarm_description = "CloudWatch Agent could not find the App ${each.value} process for three minutes."
  namespace         = "CWAgent"
  metric_name       = "procstat_lookup_pid_count"
  dimensions = {
    InstanceId = aws_instance.app.id
    exe        = each.value
    pid_finder = "native"
  }
  statistic           = "Minimum"
  period              = 60
  evaluation_periods  = 3
  datapoints_to_alarm = 3
  comparison_operator = "LessThanThreshold"
  threshold           = 1
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.operations_alarm_actions
  ok_actions          = local.operations_alarm_actions

  tags = {
    Role = "observability"
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

resource "aws_cloudwatch_dashboard" "operations" {
  dashboard_name = "${local.name_prefix}-operations"

  dashboard_body = jsonencode({
    widgets = [
      {
        type   = "text"
        x      = 0
        y      = 0
        width  = 24
        height = 2
        properties = {
          markdown = "# Yeodam V1 Operations\nApp는 상시 실행, Worker는 작업 시에만 실행됩니다. CPU와 Memory는 첫 Baseline 전까지 관측만 하며 Alarm 임계값으로 사용하지 않습니다."
        }
      },
      {
        type   = "metric"
        x      = 0
        y      = 2
        width  = 12
        height = 6
        properties = {
          title   = "App EC2"
          region  = var.aws_region
          view    = "timeSeries"
          stacked = false
          period  = 300
          stat    = "Average"
          metrics = [
            ["AWS/EC2", "CPUUtilization", "InstanceId", aws_instance.app.id, { label = "CPU %" }],
            ["AWS/EC2", "CPUCreditBalance", "InstanceId", aws_instance.app.id, { label = "CPU credit" }],
            ["AWS/EC2", "StatusCheckFailed", "InstanceId", aws_instance.app.id, { label = "Status check", stat = "Maximum" }]
          ]
        }
      },
      {
        type   = "metric"
        x      = 12
        y      = 2
        width  = 12
        height = 6
        properties = {
          title   = "Worker EC2 (running periods only)"
          region  = var.aws_region
          view    = "timeSeries"
          stacked = false
          period  = 300
          stat    = "Average"
          metrics = [
            ["AWS/EC2", "CPUUtilization", "InstanceId", aws_instance.worker.id, { label = "CPU %" }],
            ["AWS/EC2", "StatusCheckFailed", "InstanceId", aws_instance.worker.id, { label = "Status check", stat = "Maximum" }],
            ["AWS/EC2", "NetworkIn", "InstanceId", aws_instance.worker.id, { label = "Network in" }],
            ["AWS/EC2", "NetworkOut", "InstanceId", aws_instance.worker.id, { label = "Network out" }]
          ]
        }
      },
      {
        type   = "metric"
        x      = 0
        y      = 8
        width  = 12
        height = 6
        properties = {
          title   = "App Host Memory and CPU I/O"
          region  = var.aws_region
          view    = "timeSeries"
          stacked = false
          period  = 60
          stat    = "Average"
          metrics = [
            ["CWAgent", "mem_used_percent", "InstanceId", aws_instance.app.id, { label = "Memory %" }],
            ["CWAgent", "swap_used_percent", "InstanceId", aws_instance.app.id, { label = "Swap %" }],
            ["CWAgent", "cpu_usage_iowait", "InstanceId", aws_instance.app.id, "cpu", "cpu-total", { label = "CPU iowait %" }]
          ]
        }
      },
      {
        type   = "metric"
        x      = 12
        y      = 8
        width  = 12
        height = 6
        properties = {
          title   = "App Filesystem Usage"
          region  = var.aws_region
          view    = "timeSeries"
          stacked = false
          period  = 60
          stat    = "Maximum"
          yAxis   = { left = { min = 0, max = 100 } }
          metrics = [
            ["CWAgent", "disk_used_percent", "InstanceId", aws_instance.app.id, "path", "/", "fstype", "ext4", { label = "Root %" }],
            ["CWAgent", "disk_used_percent", "InstanceId", aws_instance.app.id, "path", "/var/lib/mysql", "fstype", "ext4", { label = "MySQL EBS %" }]
          ]
        }
      },
      {
        type   = "metric"
        x      = 0
        y      = 14
        width  = 12
        height = 6
        properties = {
          title   = "App Process Count"
          region  = var.aws_region
          view    = "timeSeries"
          stacked = false
          period  = 60
          stat    = "Minimum"
          metrics = [for name in ["nginx", "mysqld", "dockerd"] :
            ["CWAgent", "procstat_lookup_pid_count", "InstanceId", aws_instance.app.id, "exe", name, "process_name", name, { label = name }]
          ]
        }
      },
      {
        type   = "metric"
        x      = 12
        y      = 14
        width  = 12
        height = 6
        properties = {
          title   = "Nginx 5xx"
          region  = var.aws_region
          view    = "timeSeries"
          stacked = false
          period  = 300
          stat    = "Sum"
          metrics = [
            ["Yeodam/V1", "nginx_5xx_total", { label = "5xx responses" }]
          ]
        }
      },
      {
        type   = "log"
        x      = 0
        y      = 20
        width  = 12
        height = 6
        properties = {
          title  = "Nginx requests by route and status"
          region = var.aws_region
          view   = "table"
          query  = "SOURCE '${aws_cloudwatch_log_group.nginx_access.name}' | stats count(*) as requests by normalized_route, status | sort requests desc | limit 30"
        }
      },
      {
        type   = "log"
        x      = 12
        y      = 20
        width  = 12
        height = 6
        properties = {
          title  = "Nginx route latency"
          region = var.aws_region
          view   = "table"
          query  = "SOURCE '${aws_cloudwatch_log_group.nginx_access.name}' | stats pct(request_time * 1000, 95) as request_p95_ms, pct(upstream_response_time * 1000, 95) as upstream_p95_ms by normalized_route | sort request_p95_ms desc | limit 30"
        }
      },
      {
        type   = "alarm"
        x      = 0
        y      = 26
        width  = 24
        height = 6
        properties = {
          title = "V1 Alarm Status"
          alarms = concat(
            [
              aws_cloudwatch_metric_alarm.app_status_check.arn,
              aws_cloudwatch_metric_alarm.worker_status_check.arn,
              aws_cloudwatch_metric_alarm.nginx_5xx.arn
            ],
            [for alarm in aws_cloudwatch_metric_alarm.app_disk_usage : alarm.arn],
            [for alarm in aws_cloudwatch_metric_alarm.app_process_missing : alarm.arn]
          )
        }
      }
    ]
  })
}
