resource "aws_s3_bucket" "load_test" {
  bucket = "yeodam-v2-staging-load-test-${data.aws_caller_identity.current.account_id}-${var.aws_region}"
  tags   = merge(local.common_tags, { Name = "yeodam-v2-staging-load-test", Role = "load-test" })

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_public_access_block" "load_test" {
  bucket                  = aws_s3_bucket.load_test.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "load_test" {
  bucket = aws_s3_bucket.load_test.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "load_test" {
  bucket = aws_s3_bucket.load_test.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "load_test" {
  bucket = aws_s3_bucket.load_test.id

  rule {
    id     = "expire-fixtures-after-one-day"
    status = "Enabled"

    filter {
      prefix = "load-test-fixtures/"
    }

    expiration {
      days = 1
    }
  }

  rule {
    id     = "expire-results-after-thirty-days"
    status = "Disabled"

    filter {
      prefix = "load-test-results/"
    }

    expiration {
      days = 30
    }
  }

  rule {
    id     = "expire-alb-logs-after-seven-days"
    status = "Disabled"

    filter {
      prefix = "alb-access-logs/"
    }

    expiration {
      days = 7
    }
  }

  rule {
    id     = "abort-incomplete-uploads"
    status = "Enabled"

    filter {}

    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
}

data "aws_iam_policy_document" "load_test_bucket" {
  statement {
    sid       = "AllowStagingAlbAccessLogs"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.load_test.arn}/alb-access-logs/AWSLogs/${data.aws_caller_identity.current.account_id}/*"]

    principals {
      type        = "Service"
      identifiers = ["logdelivery.elasticloadbalancing.amazonaws.com"]
    }

  }

  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.load_test.arn, "${aws_s3_bucket.load_test.arn}/*"]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "load_test" {
  bucket     = aws_s3_bucket.load_test.id
  policy     = data.aws_iam_policy_document.load_test_bucket.json
  depends_on = [aws_s3_bucket_public_access_block.load_test]
}
