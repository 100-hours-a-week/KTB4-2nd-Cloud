provider "aws" {
  region              = "ap-northeast-2"
  allowed_account_ids = ["483175530259"]
  default_tags { tags = { Project = "yeodam", Version = "v3", Environment = "foundation", ManagedBy = "Terraform", Issue = "557" } }
}
variable "enabled" {
  type    = bool
  default = false
}
locals {
  cluster   = "yeodam-v3-foundation"
  roles     = var.enabled ? toset(["ai-api", "ai-runner"]) : toset([])
  images    = ["arn:aws:s3:::yeodam-v3-foundation-app-data-483175530259-ap-northeast-2/trip-uploads/*/derived/*", "arn:aws:s3:::yeodam-v3-foundation-app-data-483175530259-ap-northeast-2/trip-additions/*/derived/*"]
  reference = "arn:aws:s3:::yeodam-v1-app-data-483175530259-ap-northeast-2/ai-reference/places/v2026-10-30/places.sqlite"
}
resource "aws_iam_role" "app" {
  for_each = local.roles
  name     = "${local.cluster}-${each.key}"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Allow", Principal = { Service = "pods.eks.amazonaws.com" }, Action = ["sts:AssumeRole", "sts:TagSession"], Condition = {
    StringEquals = { "aws:SourceAccount" = "483175530259", "aws:RequestTag/kubernetes-namespace" = "ai", "aws:RequestTag/kubernetes-service-account" = each.key },
    ArnEquals    = { "aws:SourceArn" = "arn:aws:eks:ap-northeast-2:483175530259:cluster/${local.cluster}" }
  } }] })
}
resource "aws_eks_pod_identity_association" "app" {
  for_each        = local.roles
  cluster_name    = local.cluster
  namespace       = "ai"
  service_account = each.key
  role_arn        = aws_iam_role.app[each.key].arn
}
resource "aws_iam_role_policy" "read" {
  for_each = local.roles
  name     = "read-v3-derived-photos"
  role     = aws_iam_role.app[each.key].id
  policy   = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Allow", Action = ["s3:GetObject"], Resource = concat(local.images, each.key == "ai-runner" ? [local.reference] : []) }] })
}
