data "aws_ami" "ubuntu_arm64" {
  owners      = ["099720109477"]
  most_recent = false

  filter {
    name   = "image-id"
    values = [var.ami_id]
  }

  filter {
    name   = "architecture"
    values = ["arm64"]
  }

  filter {
    name   = "root-device-type"
    values = ["ebs"]
  }
}

resource "aws_security_group" "mysql" {
  name_prefix = "${var.name_prefix}-mysql-"
  description = "MySQL access from V2 backend tasks only"
  vpc_id      = var.vpc_id

  tags = merge(var.tags, { Name = "${var.name_prefix}-mysql-sg" })
}

resource "aws_vpc_security_group_ingress_rule" "backend_mysql" {
  security_group_id            = aws_security_group.mysql.id
  referenced_security_group_id = var.backend_security_group_id
  description                  = "MySQL from Backend tasks"
  from_port                    = 3306
  to_port                      = 3306
  ip_protocol                  = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "https" {
  security_group_id = aws_security_group.mysql.id
  description       = "Package, registry, SSM and Secrets Manager access through NAT"
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "http_packages" {
  security_group_id = aws_security_group.mysql.id
  description       = "Ubuntu package mirrors during bootstrap"
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 80
  to_port           = 80
  ip_protocol       = "tcp"
}

resource "aws_secretsmanager_secret" "root_password" {
  name        = "${var.name_prefix}/mysql-root-password"
  description = "Root password for the V2 staging MySQL instance; value is set outside Terraform"
  tags        = var.tags
}

data "aws_iam_policy_document" "instance_assume_role" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "mysql" {
  name               = "${var.name_prefix}-mysql"
  assume_role_policy = data.aws_iam_policy_document.instance_assume_role.json
  tags               = var.tags
}

resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.mysql.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

data "aws_iam_policy_document" "read_root_password" {
  statement {
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.root_password.arn]
  }
}

resource "aws_iam_role_policy" "read_root_password" {
  name   = "${var.name_prefix}-mysql-root-password"
  role   = aws_iam_role.mysql.id
  policy = data.aws_iam_policy_document.read_root_password.json
}

resource "aws_iam_instance_profile" "mysql" {
  name = "${var.name_prefix}-mysql"
  role = aws_iam_role.mysql.name
  tags = var.tags
}

resource "aws_ebs_volume" "data" {
  availability_zone = var.availability_zone
  size              = var.data_volume_size_gib
  type              = "gp3"
  encrypted         = true

  tags = merge(var.tags, { Name = "${var.name_prefix}-mysql-data" })

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_instance" "mysql" {
  ami                         = data.aws_ami.ubuntu_arm64.id
  instance_type               = var.instance_type
  subnet_id                   = var.private_subnet_id
  vpc_security_group_ids      = [aws_security_group.mysql.id]
  iam_instance_profile        = aws_iam_instance_profile.mysql.name
  associate_public_ip_address = false
  monitoring                  = true
  disable_api_termination     = true

  metadata_options {
    http_endpoint = "enabled"
    http_tokens   = "required"
  }

  root_block_device {
    volume_type           = "gp3"
    volume_size           = 30
    encrypted             = true
    delete_on_termination = true
  }

  user_data = templatefile("${path.module}/bootstrap.sh.tftpl", {
    volume_id  = aws_ebs_volume.data.id
    secret_arn = aws_secretsmanager_secret.root_password.arn
    aws_region = var.aws_region
    image      = var.mysql_image
  })

  tags = merge(var.tags, { Name = "${var.name_prefix}-mysql" })

  depends_on = [
    aws_iam_role_policy_attachment.ssm,
    aws_iam_role_policy.read_root_password,
  ]
}

resource "aws_volume_attachment" "data" {
  device_name = "/dev/sdf"
  volume_id   = aws_ebs_volume.data.id
  instance_id = aws_instance.mysql.id
}
