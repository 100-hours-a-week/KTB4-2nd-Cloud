resource "aws_s3_bucket" "app_data" {
  bucket = "${local.name_prefix}-app-data-${data.aws_caller_identity.current.account_id}-${var.aws_region}"

  tags = {
    Name = "${local.name_prefix}-app-data"
    Role = "application-data"
  }

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_ownership_controls" "app_data" {
  bucket = aws_s3_bucket.app_data.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "app_data" {
  bucket = aws_s3_bucket.app_data.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "app_data" {
  bucket = aws_s3_bucket.app_data.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_versioning" "app_data" {
  bucket = aws_s3_bucket.app_data.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "app_data" {
  bucket = aws_s3_bucket.app_data.id

  rule {
    id     = "expire-temporary-download-archives"
    status = "Enabled"

    filter {
      and {
        prefix = "trip-downloads/"

        tags = {
          status = "temporary"
        }
      }
    }

    expiration {
      days = 1
    }

    noncurrent_version_expiration {
      noncurrent_days = 1
    }
  }

  rule {
    id     = "remove-expired-download-delete-markers"
    status = "Enabled"

    filter {
      prefix = "trip-downloads/"
    }

    expiration {
      expired_object_delete_marker = true
    }
  }

  rule {
    id     = "expire-load-test-fixtures"
    status = "Enabled"

    filter {
      prefix = "load-test-fixtures/"
    }

    expiration {
      days = 1
    }

    noncurrent_version_expiration {
      noncurrent_days = 1
    }
  }

  rule {
    id     = "remove-load-test-fixture-delete-markers"
    status = "Enabled"

    filter {
      prefix = "load-test-fixtures/"
    }

    expiration {
      expired_object_delete_marker = true
    }
  }

  rule {
    id     = "expire-load-test-results"
    status = "Enabled"

    filter {
      prefix = "load-test-results/"
    }

    expiration {
      days = 30
    }

    noncurrent_version_expiration {
      noncurrent_days = 1
    }
  }

  rule {
    id     = "remove-load-test-result-delete-markers"
    status = "Enabled"

    filter {
      prefix = "load-test-results/"
    }

    expiration {
      expired_object_delete_marker = true
    }
  }

  depends_on = [
    aws_s3_bucket_versioning.app_data,
  ]
}

data "aws_iam_policy_document" "app_data_bucket" {
  statement {
    sid    = "DenyInsecureTransport"
    effect = "Deny"

    actions = ["s3:*"]

    resources = [
      aws_s3_bucket.app_data.arn,
      "${aws_s3_bucket.app_data.arn}/*",
    ]

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

resource "aws_s3_bucket_policy" "app_data" {
  bucket = aws_s3_bucket.app_data.id
  policy = data.aws_iam_policy_document.app_data_bucket.json

  depends_on = [
    aws_s3_bucket_public_access_block.app_data,
  ]
}
