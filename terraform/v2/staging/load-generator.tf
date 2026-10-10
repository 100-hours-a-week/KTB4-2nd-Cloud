locals {
  load_generator_name = "yeodam-v2-staging-load-generator"
}

# Test assets and results must never share the production load-test bucket.
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
    status = "Enabled"

    filter {
      prefix = "load-test-results/"
    }

    expiration {
      days = 30
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

resource "aws_iam_role" "load_generator" {
  count              = var.enable_load_generator ? 1 : 0
  name               = local.load_generator_name
  assume_role_policy = data.aws_iam_policy_document.v1_source_ec2_assume_role.json
  tags               = merge(local.common_tags, { Role = "load-generator", Temporary = "true" })
}

resource "aws_iam_role_policy_attachment" "load_generator_ssm" {
  count      = var.enable_load_generator ? 1 : 0
  role       = aws_iam_role.load_generator[0].name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

data "aws_iam_policy_document" "load_generator_s3" {
  statement {
    sid       = "ListOwnLoadTestPrefixes"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.load_test.arn]

    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["load-test-fixtures/*", "load-test-results/*"]
    }
  }

  statement {
    sid       = "ReadFixtures"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.load_test.arn}/load-test-fixtures/*"]
  }

  statement {
    sid       = "WriteResults"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.load_test.arn}/load-test-results/*"]
  }
}

resource "aws_iam_role_policy" "load_generator_s3" {
  count  = var.enable_load_generator ? 1 : 0
  name   = "${local.load_generator_name}-s3"
  role   = aws_iam_role.load_generator[0].id
  policy = data.aws_iam_policy_document.load_generator_s3.json
}

resource "aws_iam_instance_profile" "load_generator" {
  count = var.enable_load_generator ? 1 : 0
  name  = local.load_generator_name
  role  = aws_iam_role.load_generator[0].name
  tags  = merge(local.common_tags, { Role = "load-generator", Temporary = "true" })
}

resource "aws_security_group" "load_generator" {
  count       = var.enable_load_generator ? 1 : 0
  name_prefix = "${local.load_generator_name}-"
  description = "Temporary staging k6 host with SSM access and no inbound rules"
  vpc_id      = module.network.vpc_id
  tags        = merge(local.common_tags, { Name = "${local.load_generator_name}-sg", Role = "load-generator", Temporary = "true" })
}

resource "aws_vpc_security_group_egress_rule" "load_generator_https" {
  count             = var.enable_load_generator ? 1 : 0
  security_group_id = aws_security_group.load_generator[0].id
  description       = "SSM, package downloads, S3 and staging HTTPS endpoint"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_instance" "load_generator" {
  count                       = var.enable_load_generator ? 1 : 0
  ami                         = data.aws_ami.v1_source_ubuntu.id
  instance_type               = var.load_generator_instance_type
  subnet_id                   = module.network.public_subnet_ids["app"]
  vpc_security_group_ids      = [aws_security_group.load_generator[0].id]
  iam_instance_profile        = aws_iam_instance_profile.load_generator[0].name
  associate_public_ip_address = true
  monitoring                  = true
  disable_api_termination     = false
  user_data                   = file("${path.module}/../../../scripts/bootstrap-load-generator.sh")
  user_data_replace_on_change = true

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  root_block_device {
    volume_type           = "gp3"
    volume_size           = 16
    encrypted             = true
    delete_on_termination = true
  }

  tags = merge(local.common_tags, { Name = local.load_generator_name, Role = "load-generator", Temporary = "true" })

  depends_on = [aws_iam_role_policy_attachment.load_generator_ssm, aws_iam_role_policy.load_generator_s3]
}
