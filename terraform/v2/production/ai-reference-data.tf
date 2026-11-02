# Shared reference data, independent of existing staging/production photo policies.
locals {
  ai_reference_access = jsondecode(file("${path.module}/../../../deploy/ai-reference/policies.json"))
}

resource "aws_iam_role_policy" "ai_reference_data_read" {
  name   = "ai-reference-data-read"
  role   = data.aws_iam_role.shared_gpu.name
  policy = jsonencode(local.ai_reference_access.runner)

  lifecycle {
    precondition {
      condition     = data.aws_caller_identity.current.account_id == "483175530259" && data.aws_s3_bucket.production_photos.id == local.ai_reference_access.bucket
      error_message = "AI reference data access is limited to the reviewed Yeodam account and bucket."
    }
  }
}

output "ai_place_reference_s3_uri" {
  value = "s3://${local.ai_reference_access.bucket}/${local.ai_reference_access.prefix}"
}
