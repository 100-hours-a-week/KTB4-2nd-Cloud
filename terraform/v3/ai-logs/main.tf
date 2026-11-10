provider "aws" {
  region              = "ap-northeast-2"
  allowed_account_ids = ["483175530259"]
  default_tags { tags = { Project = "yeodam", Version = "v3", Environment = "foundation", ManagedBy = "Terraform", Issue = "623" } }
}
variable "enabled" {
  type    = bool
  default = false
}
locals {
  account        = "483175530259"
  cluster        = "yeodam-v3-foundation"
  groups         = toset(["/yeodam/v3/production/ai-api", "/yeodam/v3/production/ai-runner"])
  group_arns     = [for name in sort(tolist(local.groups)) : "arn:aws:logs:ap-northeast-2:${local.account}:log-group:${name}"]
  stream_arns    = [for arn in local.group_arns : "${arn}:log-stream:*"]
  instance_arn   = "arn:aws:sso:::instance/ssoins-72307cb6e4ae95b6"
  permission_set = "arn:aws:sso:::permissionSet/ssoins-72307cb6e4ae95b6/ps-72300040836e1a9e"
}
resource "aws_cloudwatch_log_group" "ai" {
  for_each          = var.enabled ? local.groups : toset([])
  name              = each.key
  retention_in_days = 7
  lifecycle { prevent_destroy = true }
}
resource "aws_iam_role" "collector" {
  count = var.enabled ? 1 : 0
  name  = "yeodam-v3-foundation-ai-log-collector"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Principal = { Service = "pods.eks.amazonaws.com" }, Action = ["sts:AssumeRole", "sts:TagSession"],
    Condition = { StringEquals = { "aws:SourceAccount" = local.account, "aws:RequestTag/eks-cluster-name" = local.cluster,
    "aws:RequestTag/kubernetes-namespace" = "observability", "aws:RequestTag/kubernetes-service-account" = "ai-log-collector" } }
  }] })
}
resource "aws_iam_role_policy" "collector" {
  count = var.enabled ? 1 : 0
  name  = "write-only-v3-ai-logs"
  role  = aws_iam_role.collector[0].id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"], Resource = local.stream_arns },
    { Effect = "Allow", Action = ["logs:DescribeLogStreams"], Resource = local.group_arns }
  ] })
}
resource "aws_eks_pod_identity_association" "collector" {
  count           = var.enabled ? 1 : 0
  cluster_name    = local.cluster
  namespace       = "observability"
  service_account = "ai-log-collector"
  role_arn        = aws_iam_role.collector[0].arn
}
resource "aws_iam_policy" "reader" {
  count = var.enabled ? 1 : 0
  name  = "YeodamV3AILogReader"
  path  = "/"
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["logs:DescribeLogGroups", "logs:DescribeQueries", "logs:GetQueryResults", "logs:StopQuery"], Resource = "*" },
    { Effect = "Allow", Action = ["logs:DescribeLogStreams", "logs:StartQuery", "logs:FilterLogEvents", "logs:GetLogEvents", "logs:GetLogGroupFields", "logs:GetLogRecord"],
    Resource = concat(local.group_arns, [for arn in local.group_arns : "${arn}:*"]) }
  ] })
}
# Add one customer policy reference, preserving the existing SSO inline policy.
# The reviewed permission set is provisioned to this account only.
resource "aws_ssoadmin_customer_managed_policy_attachment" "reader" {
  count              = var.enabled ? 1 : 0
  instance_arn       = local.instance_arn
  permission_set_arn = local.permission_set
  customer_managed_policy_reference {
    name = aws_iam_policy.reader[0].name
    path = "/"
  }
}
output "log_groups" { value = [for group in aws_cloudwatch_log_group.ai : group.name] }
output "collector_role_arn" { value = try(aws_iam_role.collector[0].arn, null) }
