resource "aws_s3_bucket" "mysql_backup" {
  bucket = "yeodam-v2-staging-mysql-backup-${data.aws_caller_identity.current.account_id}-${var.aws_region}"

  tags = merge(local.common_tags, { Name = "yeodam-v2-staging-mysql-backup", Role = "mysql-backup" })

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_public_access_block" "mysql_backup" {
  bucket                  = aws_s3_bucket.mysql_backup.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "mysql_backup" {
  bucket = aws_s3_bucket.mysql_backup.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "mysql_backup" {
  bucket = aws_s3_bucket.mysql_backup.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_versioning" "mysql_backup" {
  bucket = aws_s3_bucket.mysql_backup.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "mysql_backup" {
  bucket = aws_s3_bucket.mysql_backup.id

  rule {
    id     = "expire-backup-objects-after-14-days"
    status = "Enabled"

    filter {}

    expiration {
      days = 14
    }

    noncurrent_version_expiration {
      noncurrent_days = 1
    }
  }

  rule {
    id     = "remove-expired-backup-delete-markers"
    status = "Enabled"

    filter {}

    expiration {
      expired_object_delete_marker = true
    }
  }

  rule {
    id     = "abort-incomplete-backup-uploads"
    status = "Enabled"

    filter {}

    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }

  depends_on = [aws_s3_bucket_versioning.mysql_backup]
}

data "aws_iam_policy_document" "mysql_backup_bucket" {
  statement {
    sid    = "DenyInsecureTransport"
    effect = "Deny"

    actions = ["s3:*"]
    resources = [
      aws_s3_bucket.mysql_backup.arn,
      "${aws_s3_bucket.mysql_backup.arn}/*",
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

resource "aws_s3_bucket_policy" "mysql_backup" {
  bucket     = aws_s3_bucket.mysql_backup.id
  policy     = data.aws_iam_policy_document.mysql_backup_bucket.json
  depends_on = [aws_s3_bucket_public_access_block.mysql_backup]
}

resource "aws_cloudwatch_metric_alarm" "mysql_binlog_ship_warning" {
  alarm_name          = "yeodam-v2-staging-mysql-binlog-shipping-warning"
  alarm_description   = "No complete external binary log shipment for at least three minutes"
  namespace           = "Yeodam/V2/Staging/MySQL"
  metric_name         = "ExternalBinlogAgeSeconds"
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 1
  threshold           = 180
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "breaching"
  tags                = local.common_tags
}

resource "aws_cloudwatch_metric_alarm" "mysql_binlog_ship_critical" {
  alarm_name          = "yeodam-v2-staging-mysql-binlog-shipping-critical"
  alarm_description   = "No complete external binary log shipment for five minutes; RPO target at risk"
  namespace           = "Yeodam/V2/Staging/MySQL"
  metric_name         = "ExternalBinlogAgeSeconds"
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 1
  threshold           = 300
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "breaching"
  tags                = local.common_tags
}
