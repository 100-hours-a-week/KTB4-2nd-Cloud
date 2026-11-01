# Preserve rehearsal objects, release bundles and historical logs.
# No Source App, Worker, EBS, EIP or public DNS is recreated.

module "v1_source_storage" {
  source = "../modules/app-storage"

  bucket_name    = "yeodam-v2-stg-v1-app-${data.aws_caller_identity.current.account_id}-${var.aws_region}"
  browser_origin = "https://v1-staging.yeodam-2gether.com"
  tags           = merge(local.common_tags, { Role = "v1-source" })
}

resource "aws_cloudwatch_log_group" "v1_source_backend" {
  name              = "/yeodam/v2/staging/v1-source/backend"
  retention_in_days = 14
  tags              = local.common_tags
}

resource "aws_cloudwatch_log_group" "v1_source_ai" {
  name              = "/yeodam/v2/staging/v1-source/ai"
  retention_in_days = 14
  tags              = local.common_tags
}
