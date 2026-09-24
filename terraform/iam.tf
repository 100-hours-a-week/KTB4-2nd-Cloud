data "aws_iam_policy_document" "ec2_assume_role" {
  statement {
    sid     = "AllowEC2AssumeRole"
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "app_ec2" {
  name               = "${local.name_prefix}-app-ec2-role"
  description        = "IAM role for the Yeodam V1 App EC2 instance"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume_role.json

  tags = {
    Name = "${local.name_prefix}-app-ec2-role"
    Role = "app"
  }
}

resource "aws_iam_role" "worker_ec2" {
  name               = "${local.name_prefix}-worker-ec2-role"
  description        = "IAM role for the Yeodam V1 Worker EC2 instance"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume_role.json

  tags = {
    Name = "${local.name_prefix}-worker-ec2-role"
    Role = "worker"
  }
}

resource "aws_iam_role_policy_attachment" "app_ssm" {
  role       = aws_iam_role.app_ec2.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy_attachment" "app_cloudwatch_agent" {
  role       = aws_iam_role.app_ec2.name
  policy_arn = "arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy"
}

resource "aws_iam_role_policy_attachment" "worker_ssm" {
  role       = aws_iam_role.worker_ec2.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "app" {
  name = "${local.name_prefix}-app-instance-profile"
  role = aws_iam_role.app_ec2.name

  tags = {
    Name = "${local.name_prefix}-app-instance-profile"
    Role = "app"
  }
}

resource "aws_iam_instance_profile" "worker" {
  name = "${local.name_prefix}-worker-instance-profile"
  role = aws_iam_role.worker_ec2.name

  tags = {
    Name = "${local.name_prefix}-worker-instance-profile"
    Role = "worker"
  }
}

data "aws_iam_policy_document" "app_s3" {
  statement {
    sid    = "ReadBucketMetadata"
    effect = "Allow"

    actions = [
      "s3:GetBucketLocation",
      "s3:ListBucketMultipartUploads",
    ]

    resources = [
      aws_s3_bucket.app_data.arn,
    ]
  }

  statement {
    sid    = "ReadAndWriteApplicationObjects"
    effect = "Allow"

    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:PutObjectTagging",
      "s3:DeleteObject",
      "s3:AbortMultipartUpload",
      "s3:ListMultipartUploadParts",
    ]

    resources = [
      "${aws_s3_bucket.app_data.arn}/trip-uploads/*",
    ]
  }
}

resource "aws_iam_role_policy" "app_s3" {
  name   = "${local.name_prefix}-app-s3-access"
  role   = aws_iam_role.app_ec2.id
  policy = data.aws_iam_policy_document.app_s3.json
}

data "aws_iam_policy_document" "app_worker_ec2_control" {
  statement {
    sid    = "DescribeWorkerInstance"
    effect = "Allow"

    actions = [
      "ec2:DescribeInstances",
    ]

    resources = ["*"]

    condition {
      test     = "StringEquals"
      variable = "aws:RequestedRegion"
      values   = [var.aws_region]
    }
  }

  statement {
    sid    = "StartWorkerInstance"
    effect = "Allow"

    actions = [
      "ec2:StartInstances",
    ]

    resources = [
      aws_instance.worker.arn,
    ]
  }
}

resource "aws_iam_role_policy" "app_worker_ec2_control" {
  name   = "${local.name_prefix}-app-worker-ec2-control"
  role   = aws_iam_role.app_ec2.id
  policy = data.aws_iam_policy_document.app_worker_ec2_control.json
}

data "aws_iam_policy_document" "app_parameter_store" {
  statement {
    sid    = "ReadAppRuntimeParameters"
    effect = "Allow"

    actions = [
      "ssm:GetParameter",
      "ssm:GetParameters",
      "ssm:GetParametersByPath",
    ]

    resources = [
      "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter${local.parameter_store_path_prefix}/app",
      "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter${local.parameter_store_path_prefix}/app/*",
      "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter${local.parameter_store_path_prefix}/deploy",
      "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter${local.parameter_store_path_prefix}/deploy/*",
    ]
  }
}

resource "aws_iam_role_policy" "app_parameter_store" {
  name   = "${local.name_prefix}-app-parameter-store-read"
  role   = aws_iam_role.app_ec2.id
  policy = data.aws_iam_policy_document.app_parameter_store.json
}

data "aws_iam_policy_document" "worker_s3" {
  statement {
    sid    = "ReadApplicationObjects"
    effect = "Allow"

    actions = [
      "s3:GetObject",
    ]

    resources = [
      "${aws_s3_bucket.app_data.arn}/trip-uploads/*",
    ]
  }
}

resource "aws_iam_role_policy" "worker_s3" {
  name   = "${local.name_prefix}-worker-s3-access"
  role   = aws_iam_role.worker_ec2.id
  policy = data.aws_iam_policy_document.worker_s3.json
}

data "aws_iam_policy_document" "worker_parameter_store" {
  statement {
    sid    = "ReadWorkerRuntimeParameters"
    effect = "Allow"

    actions = [
      "ssm:GetParameter",
      "ssm:GetParameters",
      "ssm:GetParametersByPath",
    ]

    resources = [
      "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter${local.parameter_store_path_prefix}/worker",
      "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter${local.parameter_store_path_prefix}/worker/*",
      "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter${local.parameter_store_path_prefix}/deploy",
      "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter${local.parameter_store_path_prefix}/deploy/*",
    ]
  }
}

resource "aws_iam_role_policy" "worker_parameter_store" {
  name   = "${local.name_prefix}-worker-parameter-store-read"
  role   = aws_iam_role.worker_ec2.id
  policy = data.aws_iam_policy_document.worker_parameter_store.json
}
