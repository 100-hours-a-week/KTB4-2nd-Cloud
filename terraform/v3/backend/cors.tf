variable "operating_origin" {
  type    = bool
  default = false
}
# Browser access to presigned V3 objects only; this grants no S3 object permissions.
resource "aws_s3_bucket_cors_configuration" "qa" {
  count  = var.enabled ? 1 : 0
  bucket = local.bucket
  cors_rule {
    id              = "v3-browser-qa"
    allowed_origins = var.operating_origin ? ["https://v3.yeodam-2gether.com", "https://yeodam-2gether.com"] : ["https://v3.yeodam-2gether.com"]
    allowed_methods = ["GET", "HEAD", "PUT", "POST"]
    allowed_headers = ["*"]
    expose_headers  = ["ETag", "x-amz-request-id"]
    max_age_seconds = 600
  }
  depends_on = [aws_s3_bucket.app]
}
