provider "aws" {
  region              = "ap-northeast-2"
  allowed_account_ids = ["483175530259"]
  default_tags { tags = { Project = "yeodam", Version = "v3", Environment = "foundation", ManagedBy = "Terraform" } }
}
variable "enabled" {
  type    = bool
  default = false
}
variable "system_instance_ids" {
  type    = list(string)
  default = []
  validation {
    condition     = length(var.system_instance_ids) <= 2 && length(distinct(var.system_instance_ids)) == length(var.system_instance_ids) && alltrue([for id in var.system_instance_ids : can(regex("^i-[0-9a-f]{17}$", id))])
    error_message = "At most two exact existing system instance IDs."
  }
}
data "aws_iam_openid_connect_provider" "github" {
  count = var.enabled ? 1 : 0
  url   = "https://token.actions.githubusercontent.com"
}
data "aws_eks_cluster" "v3" {
  count = var.enabled ? 1 : 0
  name  = "yeodam-v3-foundation"
}
resource "aws_iam_role" "status" {
  count = var.enabled ? 1 : 0
  name  = "yeodam-v3-foundation-runpod-update"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Allow", Principal = { Federated = data.aws_iam_openid_connect_provider.github[0].arn }, Action = "sts:AssumeRoleWithWebIdentity", Condition = {
    StringEquals = { "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com", "token.actions.githubusercontent.com:sub" = "repo:100-hours-a-week@167328634/KTB4-2nd-Cloud@1344655631:environment:v3-production" }
    StringLike   = { "sts:RoleSessionName" = "v3-runpod-*" }
  } }] })
  lifecycle {
    precondition {
      condition     = length(var.system_instance_ids) >= 1
      error_message = "Select existing system nodes explicitly; never create a runner/node here."
    }
    precondition {
      condition     = data.aws_eks_cluster.v3[0].vpc_config[0].vpc_id == "vpc-0ecc3132f3b2170e9"
      error_message = "Only the isolated V3 cluster."
    }
  }
}
resource "aws_ssm_document" "status" {
  count           = var.enabled ? 1 : 0
  name            = "Yeodam-V3-RunPod-Update"
  document_type   = "Session"
  document_format = "JSON"
  content = jsonencode({ schemaVersion = "1.0", description = "V3 model URL management TLS tunnel", sessionType = "Port", inputs = { idleSessionTimeout = "5", maxSessionDuration = "60" }, properties = {
    host = trimprefix(data.aws_eks_cluster.v3[0].endpoint, "https://"), portNumber = "443", localPortNumber = "18574", type = "LocalPortForwarding"
  } })
}
data "aws_secretsmanager_secret" "runtime" {
  count = var.enabled ? 1 : 0
  name  = "yeodam/v3/foundation/ai/runtime"
}
resource "aws_iam_role_policy" "status" {
  count      = var.enabled ? 1 : 0
  name       = "ai-url-update-only"
  role       = "yeodam-v3-foundation-runpod-update"
  depends_on = [aws_iam_role.status, aws_ssm_document.status]
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret", "secretsmanager:PutSecretValue", "secretsmanager:UpdateSecretVersionStage"], Resource = data.aws_secretsmanager_secret.runtime[0].arn },
    { Effect = "Allow", Action = ["eks:DescribeCluster"], Resource = "arn:aws:eks:ap-northeast-2:483175530259:cluster/yeodam-v3-foundation" },
    { Effect = "Allow", Action = ["ssm:StartSession"], Resource = concat([for id in var.system_instance_ids : "arn:aws:ec2:ap-northeast-2:483175530259:instance/${id}"], ["arn:aws:ssm:ap-northeast-2:483175530259:document/Yeodam-V3-RunPod-Update"]) },
    { Effect = "Allow", Action = ["ssmmessages:OpenDataChannel"], Resource = "arn:aws:ssm:ap-northeast-2:483175530259:session/v3-runpod-*" },
    { Effect = "Allow", Action = ["ssm:TerminateSession"], Resource = "arn:aws:ssm:ap-northeast-2:483175530259:session/*", Condition = { StringEquals = { "ssm:resourceTag/aws:ssmmessages:session-id" = "$${aws:userid}" } } }
  ] })
}
resource "aws_eks_access_entry" "status" {
  count             = var.enabled ? 1 : 0
  cluster_name      = "yeodam-v3-foundation"
  principal_arn     = "arn:aws:iam::483175530259:role/yeodam-v3-foundation-runpod-update"
  type              = "STANDARD"
  kubernetes_groups = ["yeodam-v3-runpod-updater"]
  depends_on        = [aws_iam_role.status]
}
