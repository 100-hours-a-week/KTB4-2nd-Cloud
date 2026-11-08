# Independent state. No EC2/GPU/V2/Secret values or cross-state reads.
provider "aws" {
  region              = "ap-northeast-2"
  allowed_account_ids = ["483175530259"]
  default_tags { tags = { Project = "yeodam", Version = "v3", Environment = "foundation", ManagedBy = "Terraform", Issue = "544" } }
}
variable "enabled" {
  type    = bool
  default = false
}
locals {
  cluster = "yeodam-v3-foundation"
  bucket  = "yeodam-v3-foundation-app-data-483175530259-ap-northeast-2"
  roles   = var.enabled ? { auth-api = "auth", trip-api = "trip", photo-api = "photo", photo-converter = "photo", trip-result-worker = "trip" } : {}
  readers = { for k, v in local.roles : k => v if k != "auth-api" }
  writers = toset(["trip-api", "photo-api", "photo-converter"])
}
resource "aws_s3_bucket" "app" {
  count         = var.enabled ? 1 : 0
  bucket        = local.bucket
  force_destroy = false
  lifecycle { prevent_destroy = true }
}
resource "aws_s3_bucket_public_access_block" "app" {
  count                   = var.enabled ? 1 : 0
  bucket                  = aws_s3_bucket.app[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
resource "aws_s3_bucket_server_side_encryption_configuration" "app" {
  count  = var.enabled ? 1 : 0
  bucket = aws_s3_bucket.app[0].id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}
resource "aws_s3_bucket_versioning" "app" {
  count  = var.enabled ? 1 : 0
  bucket = aws_s3_bucket.app[0].id
  versioning_configuration { status = "Enabled" }
}
resource "aws_s3_bucket_policy" "tls" {
  count  = var.enabled ? 1 : 0
  bucket = aws_s3_bucket.app[0].id
  policy = jsonencode({ Version = "2012-10-17", Statement = [{ Sid = "RequireTLS", Effect = "Deny", Principal = "*", Action = "s3:*", Resource = ["arn:aws:s3:::${local.bucket}", "arn:aws:s3:::${local.bucket}/*"], Condition = { Bool = { "aws:SecureTransport" = "false" } } }] })
}
resource "aws_iam_role" "app" {
  for_each = local.roles
  name     = "${local.cluster}-be-${each.key}"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Allow", Principal = { Service = "pods.eks.amazonaws.com" }, Action = ["sts:AssumeRole", "sts:TagSession"], Condition = {
    StringEquals = { "aws:SourceAccount" = "483175530259", "aws:RequestTag/kubernetes-namespace" = each.value, "aws:RequestTag/kubernetes-service-account" = each.key },
    ArnEquals    = { "aws:SourceArn" = "arn:aws:eks:ap-northeast-2:483175530259:cluster/${local.cluster}" }
  } }] })
}
resource "aws_eks_pod_identity_association" "app" {
  for_each        = local.roles
  cluster_name    = local.cluster
  namespace       = each.value
  service_account = each.key
  role_arn        = aws_iam_role.app[each.key].arn
}
resource "aws_iam_role_policy" "s3" {
  for_each = local.readers
  role     = aws_iam_role.app[each.key].id
  name     = "v3-app-data"
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["s3:ListBucket", "s3:GetBucketLocation", "s3:ListBucketMultipartUploads"], Resource = ["arn:aws:s3:::${local.bucket}"] },
    { Effect = "Allow", Action = concat(["s3:GetObject", "s3:GetObjectVersion"], contains(local.writers, each.key) ? ["s3:PutObject", "s3:DeleteObject", "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"] : []), Resource = ["arn:aws:s3:::${local.bucket}/*"] }
  ] })
}
output "bucket_name" { value = var.enabled ? local.bucket : null }
