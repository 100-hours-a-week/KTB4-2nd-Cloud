resource "aws_iam_role" "load_generator" {
  count = var.enable_load_generator ? 1 : 0

  name               = "${local.name_prefix}-load-generator-role"
  description        = "Temporary IAM role for the Yeodam V1 k6 load generator"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume_role.json

  tags = {
    Name      = "${local.name_prefix}-load-generator-role"
    Role      = "load-generator"
    Temporary = "true"
  }
}

resource "aws_iam_role_policy_attachment" "load_generator_ssm" {
  count = var.enable_load_generator ? 1 : 0

  role       = aws_iam_role.load_generator[0].name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

data "aws_iam_policy_document" "load_generator_s3" {
  statement {
    sid    = "ListLoadTestFixtures"
    effect = "Allow"

    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.app_data.arn]

    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values = [
        "load-test-fixtures/*",
        "load-test-results/*",
      ]
    }
  }

  statement {
    sid    = "ReadLoadTestFixtures"
    effect = "Allow"

    actions = ["s3:GetObject"]
    resources = [
      "${aws_s3_bucket.app_data.arn}/load-test-fixtures/*",
    ]
  }


  statement {
    sid    = "WriteLoadTestResults"
    effect = "Allow"

    actions = ["s3:PutObject"]
    resources = [
      "${aws_s3_bucket.app_data.arn}/load-test-results/*",
    ]
  }
}

resource "aws_iam_role_policy" "load_generator_s3" {
  count = var.enable_load_generator ? 1 : 0

  name   = "${local.name_prefix}-load-generator-s3"
  role   = aws_iam_role.load_generator[0].id
  policy = data.aws_iam_policy_document.load_generator_s3.json
}

resource "aws_iam_instance_profile" "load_generator" {
  count = var.enable_load_generator ? 1 : 0

  name = "${local.name_prefix}-load-generator-instance-profile"
  role = aws_iam_role.load_generator[0].name

  tags = {
    Name      = "${local.name_prefix}-load-generator-instance-profile"
    Role      = "load-generator"
    Temporary = "true"
  }
}

resource "aws_security_group" "load_generator" {
  count = var.enable_load_generator ? 1 : 0

  name                   = "${local.name_prefix}-load-generator-sg"
  description            = "Security group for the temporary Yeodam V1 k6 load generator"
  vpc_id                 = aws_vpc.main.id
  revoke_rules_on_delete = true

  tags = {
    Name      = "${local.name_prefix}-load-generator-sg"
    Role      = "load-generator"
    Temporary = "true"
  }
}

resource "aws_vpc_security_group_egress_rule" "load_generator_all_ipv4" {
  count = var.enable_load_generator ? 1 : 0

  security_group_id = aws_security_group.load_generator[0].id
  description       = "Allow temporary load generator outbound IPv4 traffic"
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_instance" "load_generator" {
  count = var.enable_load_generator ? 1 : 0

  ami                         = data.aws_ami.ubuntu_2404.id
  instance_type               = var.load_generator_instance_type
  subnet_id                   = aws_subnet.public.id
  vpc_security_group_ids      = [aws_security_group.load_generator[0].id]
  iam_instance_profile        = aws_iam_instance_profile.load_generator[0].name
  associate_public_ip_address = true

  monitoring                           = true
  disable_api_termination              = false
  instance_initiated_shutdown_behavior = "stop"

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
    http_protocol_ipv6          = "disabled"
    instance_metadata_tags      = "disabled"
  }

  root_block_device {
    volume_type           = "gp3"
    volume_size           = var.load_generator_root_volume_size
    iops                  = 3000
    throughput            = 125
    encrypted             = true
    delete_on_termination = true
  }

  user_data                   = file("${path.module}/../scripts/bootstrap-load-generator.sh")
  user_data_replace_on_change = true

  volume_tags = {
    Name      = "${local.name_prefix}-load-generator-root"
    Role      = "load-generator"
    Temporary = "true"
  }

  tags = {
    Name      = "${local.name_prefix}-load-generator"
    Role      = "load-generator"
    Temporary = "true"
  }

  depends_on = [
    aws_iam_role_policy_attachment.load_generator_ssm,
    aws_iam_role_policy.load_generator_s3,
  ]
}
