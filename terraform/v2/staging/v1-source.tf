# The rehearsal source has its own App host, MySQL volume, objects and roles.
# It shares the staging VPC only so the later DB migration can be tested without
# introducing a production-to-staging network path.
locals {
  v1_source_name             = "yeodam-v2-staging-v1-source"
  v1_source_parameter_prefix = "/yeodam/v2/staging/v1-source"
}

data "aws_ami" "v1_source_ubuntu" {
  owners      = ["099720109477"]
  most_recent = false

  filter {
    name   = "image-id"
    values = [var.v1_source_ami_id]
  }

  filter {
    name   = "architecture"
    values = ["x86_64"]
  }

  filter {
    name   = "root-device-type"
    values = ["ebs"]
  }
}

module "v1_source_storage" {
  source = "../modules/app-storage"

  bucket_name    = "yeodam-v2-stg-v1-app-${data.aws_caller_identity.current.account_id}-${var.aws_region}"
  browser_origin = "https://v1-staging.yeodam-2gether.com"
  tags           = merge(local.common_tags, { Role = "v1-source" })
}

resource "aws_security_group" "v1_source_app" {
  name_prefix = "${local.v1_source_name}-app-"
  description = "V1 rehearsal Nginx and local MySQL host"
  vpc_id      = module.network.vpc_id
  tags        = merge(local.common_tags, { Name = "${local.v1_source_name}-app-sg", Role = "v1-source" })
}

resource "aws_vpc_security_group_ingress_rule" "v1_source_http" {
  security_group_id = aws_security_group.v1_source_app.id
  description       = "HTTP redirect and ACME challenge"
  ip_protocol       = "tcp"
  from_port         = 80
  to_port           = 80
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_vpc_security_group_ingress_rule" "v1_source_https" {
  security_group_id = aws_security_group.v1_source_app.id
  description       = "V1 rehearsal browser traffic"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_vpc_security_group_egress_rule" "v1_source_app_outbound" {
  security_group_id = aws_security_group.v1_source_app.id
  description       = "Registry, SSM, S3 and external application APIs"
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_security_group" "v1_source_worker" {
  name_prefix = "${local.v1_source_name}-worker-"
  description = "V1 rehearsal AI Worker"
  vpc_id      = module.network.vpc_id
  tags        = merge(local.common_tags, { Name = "${local.v1_source_name}-worker-sg", Role = "v1-source" })
}

resource "aws_vpc_security_group_ingress_rule" "v1_source_worker_api" {
  security_group_id            = aws_security_group.v1_source_worker.id
  referenced_security_group_id = aws_security_group.v1_source_app.id
  description                  = "AI requests from the V1 rehearsal App only"
  ip_protocol                  = "tcp"
  from_port                    = 8000
  to_port                      = 8000
}

resource "aws_vpc_security_group_egress_rule" "v1_source_worker_outbound" {
  security_group_id = aws_security_group.v1_source_worker.id
  description       = "SSM, S3 and image registry through staging NAT"
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
}

data "aws_iam_policy_document" "v1_source_ec2_assume_role" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "v1_source_app" {
  name               = "${local.v1_source_name}-app"
  assume_role_policy = data.aws_iam_policy_document.v1_source_ec2_assume_role.json
  tags               = local.common_tags
}

resource "aws_iam_role" "v1_source_worker" {
  name               = "${local.v1_source_name}-worker"
  assume_role_policy = data.aws_iam_policy_document.v1_source_ec2_assume_role.json
  tags               = local.common_tags
}

resource "aws_iam_role_policy_attachment" "v1_source_app_ssm" {
  role       = aws_iam_role.v1_source_app.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy_attachment" "v1_source_worker_ssm" {
  role       = aws_iam_role.v1_source_worker.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "v1_source_app" {
  name = "${local.v1_source_name}-app"
  role = aws_iam_role.v1_source_app.name
  tags = local.common_tags
}

resource "aws_iam_instance_profile" "v1_source_worker" {
  name = "${local.v1_source_name}-worker"
  role = aws_iam_role.v1_source_worker.name
  tags = local.common_tags
}

data "aws_iam_policy_document" "v1_source_app" {
  statement {
    sid       = "ManageOwnPhotos"
    actions   = ["s3:GetBucketLocation", "s3:ListBucketMultipartUploads"]
    resources = [module.v1_source_storage.bucket_arn]
  }

  statement {
    sid       = "ManageOwnPhotoObjects"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:PutObjectTagging", "s3:DeleteObject", "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"]
    resources = ["${module.v1_source_storage.bucket_arn}/trip-uploads/*", "${module.v1_source_storage.bucket_arn}/trip-downloads/*"]
  }

  statement {
    sid       = "ReadRehearsalRelease"
    actions   = ["s3:GetObject"]
    resources = ["${module.v1_source_storage.bucket_arn}/rehearsal-release/*"]
  }

  statement {
    sid       = "ReadOnlyOwnRuntimeParameters"
    actions   = ["ssm:GetParameter", "ssm:GetParameters", "ssm:GetParametersByPath"]
    resources = ["arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter${local.v1_source_parameter_prefix}/app/*", "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter${local.v1_source_parameter_prefix}/deploy/*"]
  }

  statement {
    sid       = "DescribeWorker"
    actions   = ["ec2:DescribeInstances"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "aws:RequestedRegion"
      values   = [var.aws_region]
    }
  }

  statement {
    sid       = "StartOwnWorker"
    actions   = ["ec2:StartInstances"]
    resources = [aws_instance.v1_source_worker.arn]
  }

  statement {
    sid       = "WriteOwnBackendLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.v1_source_backend.arn}:*"]
  }
}

resource "aws_iam_role_policy" "v1_source_app" {
  name   = "${local.v1_source_name}-app"
  role   = aws_iam_role.v1_source_app.id
  policy = data.aws_iam_policy_document.v1_source_app.json
}

data "aws_iam_policy_document" "v1_source_worker" {
  statement {
    sid       = "ReadOwnPhotos"
    actions   = ["s3:GetObject"]
    resources = ["${module.v1_source_storage.bucket_arn}/trip-uploads/*"]
  }

  statement {
    sid       = "ReadRehearsalRelease"
    actions   = ["s3:GetObject"]
    resources = ["${module.v1_source_storage.bucket_arn}/rehearsal-release/*"]
  }

  statement {
    sid       = "ReadOnlyOwnRuntimeParameters"
    actions   = ["ssm:GetParameter", "ssm:GetParameters", "ssm:GetParametersByPath"]
    resources = ["arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter${local.v1_source_parameter_prefix}/worker/*", "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter${local.v1_source_parameter_prefix}/deploy/*"]
  }

  statement {
    sid       = "WriteOwnAiLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.v1_source_ai.arn}:*"]
  }
}

resource "aws_iam_role_policy" "v1_source_worker" {
  name   = "${local.v1_source_name}-worker"
  role   = aws_iam_role.v1_source_worker.id
  policy = data.aws_iam_policy_document.v1_source_worker.json
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

resource "aws_ebs_volume" "v1_source_mysql" {
  availability_zone = var.app_availability_zone
  size              = var.v1_source_mysql_data_gib
  type              = "gp3"
  encrypted         = true
  tags              = merge(local.common_tags, { Name = "${local.v1_source_name}-mysql-data", Role = "v1-source" })

  lifecycle {
    prevent_destroy = true
  }
}

# The subnet does not auto-assign a public IP; the separate EIP supplies it.
# AWS reports the public-IP association as true after attaching the EIP.
resource "aws_instance" "v1_source_app" {
  ami                     = data.aws_ami.v1_source_ubuntu.id
  instance_type           = var.v1_source_app_instance_type
  subnet_id               = module.network.public_subnet_ids["app"]
  vpc_security_group_ids  = [aws_security_group.v1_source_app.id]
  iam_instance_profile    = aws_iam_instance_profile.v1_source_app.name
  monitoring              = true
  disable_api_termination = true

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 2
  }

  root_block_device {
    volume_type           = "gp3"
    volume_size           = 30
    encrypted             = true
    delete_on_termination = true
  }

  tags = merge(local.common_tags, { Name = "${local.v1_source_name}-app", Role = "v1-source" })

  depends_on = [aws_iam_role_policy_attachment.v1_source_app_ssm, aws_iam_role_policy.v1_source_app]
}

resource "aws_volume_attachment" "v1_source_mysql" {
  device_name = "/dev/sdf"
  volume_id   = aws_ebs_volume.v1_source_mysql.id
  instance_id = aws_instance.v1_source_app.id
}

resource "aws_eip" "v1_source_app" {
  domain = "vpc"
  tags   = merge(local.common_tags, { Name = "${local.v1_source_name}-app-eip", Role = "v1-source" })
}

resource "aws_eip_association" "v1_source_app" {
  allocation_id = aws_eip.v1_source_app.id
  instance_id   = aws_instance.v1_source_app.id
}

resource "aws_instance" "v1_source_worker" {
  ami                                  = data.aws_ami.v1_source_ubuntu.id
  instance_type                        = var.v1_source_worker_instance_type
  subnet_id                            = module.network.private_app_subnet_id
  vpc_security_group_ids               = [aws_security_group.v1_source_worker.id]
  iam_instance_profile                 = aws_iam_instance_profile.v1_source_worker.name
  associate_public_ip_address          = false
  monitoring                           = true
  instance_initiated_shutdown_behavior = "stop"

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 2
  }

  root_block_device {
    volume_type           = "gp3"
    volume_size           = 30
    encrypted             = true
    delete_on_termination = true
  }

  tags = merge(local.common_tags, { Name = "${local.v1_source_name}-worker", Role = "v1-source" })

  depends_on = [aws_iam_role_policy_attachment.v1_source_worker_ssm, aws_iam_role_policy.v1_source_worker]
}

resource "aws_route53_record" "v1_source_app" {
  zone_id = data.aws_route53_zone.public.zone_id
  name    = "v1-staging.yeodam-2gether.com"
  type    = "A"
  ttl     = 60
  records = [aws_eip.v1_source_app.public_ip]
}
