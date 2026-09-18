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