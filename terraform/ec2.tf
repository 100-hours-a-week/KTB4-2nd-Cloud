resource "aws_instance" "app" {
  ami                    = data.aws_ami.ubuntu_2404.id
  instance_type          = var.app_instance_type
  subnet_id              = aws_subnet.public.id
  vpc_security_group_ids = [aws_security_group.app.id]
  iam_instance_profile   = aws_iam_instance_profile.app.name

  monitoring                           = true
  disable_api_termination              = true
  instance_initiated_shutdown_behavior = "stop"

  credit_specification {
    cpu_credits = "unlimited"
  }

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 2
    http_protocol_ipv6          = "disabled"
    instance_metadata_tags      = "disabled"
  }

  root_block_device {
    volume_type           = "gp3"
    volume_size           = var.app_root_volume_size
    iops                  = 3000
    throughput            = 125
    encrypted             = true
    delete_on_termination = true
  }

  volume_tags = {
    Name = "${local.name_prefix}-app-root"
    Role = "app"
  }

  tags = {
    Name = "${local.name_prefix}-app"
    Role = "app"
  }

  depends_on = [
    aws_iam_role_policy_attachment.app_ssm,
    aws_iam_role_policy_attachment.app_cloudwatch_agent,
  ]

  lifecycle {
    ignore_changes = [
      ebs_block_device,
    ]
  }
}

resource "aws_eip" "app" {
  domain = "vpc"

  tags = {
    Name = "${local.name_prefix}-app-eip"
    Role = "app"
  }
}

resource "aws_eip_association" "app" {
  allocation_id = aws_eip.app.id
  instance_id   = aws_instance.app.id
}

resource "aws_instance" "worker" {
  ami                    = data.aws_ami.ubuntu_2404.id
  instance_type          = var.worker_instance_type
  subnet_id              = aws_subnet.public.id
  vpc_security_group_ids = [aws_security_group.worker.id]
  iam_instance_profile   = aws_iam_instance_profile.worker.name

  associate_public_ip_address = true

  monitoring                           = true
  disable_api_termination              = true
  instance_initiated_shutdown_behavior = "stop"

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 2
    http_protocol_ipv6          = "disabled"
    instance_metadata_tags      = "disabled"
  }

  root_block_device {
    volume_type           = "gp3"
    volume_size           = var.worker_root_volume_size
    iops                  = 3000
    throughput            = 125
    encrypted             = true
    delete_on_termination = true
  }

  volume_tags = {
    Name = "${local.name_prefix}-worker-root"
    Role = "worker"
  }

  tags = {
    Name = "${local.name_prefix}-worker"
    Role = "worker"
  }

  depends_on = [
    aws_iam_role_policy_attachment.worker_ssm,
  ]
}
