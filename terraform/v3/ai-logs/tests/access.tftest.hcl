mock_provider "aws" {}
run "disabled" {
  command = plan
  assert {
    condition     = length(aws_cloudwatch_log_group.ai) == 0 && length(aws_iam_role.collector) == 0 && length(aws_ssoadmin_customer_managed_policy_attachment.reader) == 0
    error_message = "Default must create no resources."
  }
}
run "bounded_access" {
  command = plan
  variables { enabled = true }
  assert {
    condition     = length(aws_cloudwatch_log_group.ai) == 2 && alltrue([for group in aws_cloudwatch_log_group.ai : group.retention_in_days == 7])
    error_message = "Exactly two seven-day AI groups."
  }
  assert {
    condition     = jsondecode(aws_iam_role_policy.collector[0].policy).Statement[0].Action == ["logs:CreateLogStream", "logs:PutLogEvents"] && length(jsondecode(aws_iam_role_policy.collector[0].policy).Statement[0].Resource) == 2
    error_message = "Collector cannot read/delete logs or access secrets."
  }
  assert {
    condition     = aws_eks_pod_identity_association.collector[0].namespace == "observability" && aws_eks_pod_identity_association.collector[0].service_account == "ai-log-collector"
    error_message = "Only the dedicated collector identity."
  }
  assert {
    condition     = aws_ssoadmin_customer_managed_policy_attachment.reader[0].permission_set_arn == local.permission_set && length(jsondecode(aws_iam_policy.reader[0].policy).Statement[1].Resource) == 4
    error_message = "Add only the reviewed permission set and two groups, preserving its original policy."
  }
}

run "app_logs_opt_in" {
  command = plan
  variables {
    enabled              = true
    backend_enabled      = true
    grafana_logs_enabled = true
  }
  assert {
    condition     = length(aws_cloudwatch_log_group.backend) == 1 && aws_cloudwatch_log_group.backend[0].retention_in_days == 7 && length(aws_cloudwatch_log_group.ai) == 2
    error_message = "One additional seven-day BE group; preserve the two AI groups."
  }
  assert {
    condition     = length(jsondecode(aws_iam_role_policy.collector[0].policy).Statement[0].Resource) == 3 && length(jsondecode(aws_iam_policy.reader[0].policy).Statement[1].Resource) == 4
    error_message = "Extend only the collector; never widen the existing AI SSO reader."
  }
  assert {
    condition     = jsondecode(aws_iam_role_policy.grafana_logs[0].policy).Statement[1].Action == ["logs:StartQuery", "logs:GetLogGroupFields"] && length(jsondecode(aws_iam_role_policy.grafana_logs[0].policy).Statement[1].Resource) == 6 && jsondecode(aws_iam_role_policy.grafana_logs[0].policy).Statement[0].Action == ["logs:DescribeLogGroups", "logs:GetQueryResults", "logs:StopQuery", "cloudwatch:ListMetrics"]
    error_message = "Query only the exact three application groups; no data write/secret actions."
  }
  assert {
    condition     = aws_eks_pod_identity_association.grafana_logs[0].service_account == "v3-grafana" && jsondecode(aws_iam_role.grafana_logs[0].assume_role_policy).Statement[0].Condition.StringEquals["aws:RequestTag/kubernetes-service-account"] == "v3-grafana"
    error_message = "Dedicated Grafana Pod Identity, not a node role or static key."
  }
}
