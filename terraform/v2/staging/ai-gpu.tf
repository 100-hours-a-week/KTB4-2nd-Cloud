locals {
  ai_gpu_name = "yeodam-v2-staging-ai-gpu"
}

# Reviewed AWS DLAMI: GPU driver and NVIDIA container toolkit are preinstalled.
# Pin the image rather than replacing a stateful GPU host on each AMI release.
data "aws_ami" "ai_gpu" {
  owners = ["898082745236"]
  filter {
    name   = "image-id"
    values = ["ami-0eb06241c2fe1ed0c"]
  }
  filter {
    name   = "architecture"
    values = ["x86_64"]
  }
}

resource "aws_security_group" "ai_api_tasks" {
  name_prefix = "yeodam-v2-staging-ai-api-"
  description = "Network identity for future staging AI API tasks"
  vpc_id      = module.network.vpc_id
  tags        = merge(local.common_tags, { Name = "yeodam-v2-staging-ai-api-sg", Role = "ai" })
}

resource "aws_security_group" "ai_gpu" {
  name_prefix = "${local.ai_gpu_name}-"
  description = "Private embedding and Qdrant host; SSM administration only"
  vpc_id      = module.network.vpc_id
  tags        = merge(local.common_tags, { Name = "${local.ai_gpu_name}-sg", Role = "ai" })
}

resource "aws_vpc_security_group_ingress_rule" "ai_gpu_api" {
  for_each                     = { embedding = 8000, qdrant = 6333 }
  security_group_id            = aws_security_group.ai_gpu.id
  referenced_security_group_id = aws_security_group.ai_api_tasks.id
  description                  = "AI API to ${each.key} on private GPU host"
  ip_protocol                  = "tcp"
  from_port                    = each.value
  to_port                      = each.value
}

resource "aws_vpc_security_group_egress_rule" "ai_api_gpu" {
  for_each                     = { embedding = 8000, qdrant = 6333 }
  security_group_id            = aws_security_group.ai_api_tasks.id
  referenced_security_group_id = aws_security_group.ai_gpu.id
  description                  = "AI API to private ${each.key}"
  ip_protocol                  = "tcp"
  from_port                    = each.value
  to_port                      = each.value
}

resource "aws_vpc_security_group_egress_rule" "ai_gpu_https" {
  security_group_id = aws_security_group.ai_gpu.id
  description       = "SSM and HTTPS registry/model downloads through staging NAT"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_iam_role" "ai_gpu" {
  name               = local.ai_gpu_name
  assume_role_policy = data.aws_iam_policy_document.v1_source_ec2_assume_role.json
  tags               = merge(local.common_tags, { Role = "ai" })
}

resource "aws_iam_role_policy_attachment" "ai_gpu_ssm" {
  role       = aws_iam_role.ai_gpu.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "ai_gpu" {
  name = local.ai_gpu_name
  role = aws_iam_role.ai_gpu.name
  tags = merge(local.common_tags, { Role = "ai" })
}

resource "aws_ebs_volume" "qdrant_data" {
  availability_zone = var.app_availability_zone
  size              = 20
  type              = "gp3"
  encrypted         = true
  tags              = merge(local.common_tags, { Name = "${local.ai_gpu_name}-qdrant-data", Role = "ai" })
  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_instance" "ai_gpu" {
  ami                         = data.aws_ami.ai_gpu.id
  instance_type               = "g6.xlarge"
  subnet_id                   = module.network.private_app_subnet_id
  vpc_security_group_ids      = [aws_security_group.ai_gpu.id, aws_security_group.ai_kafka_clients.id]
  iam_instance_profile        = aws_iam_instance_profile.ai_gpu.name
  associate_public_ip_address = false
  disable_api_termination     = true
  user_data                   = templatefile("${path.module}/ai-gpu-bootstrap.sh.tftpl", { volume_id = aws_ebs_volume.qdrant_data.id })

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 2
  }

  root_block_device {
    volume_type           = "gp3"
    volume_size           = 100
    encrypted             = true
    delete_on_termination = true
  }

  tags       = merge(local.common_tags, { Name = local.ai_gpu_name, Role = "ai" })
  depends_on = [module.network, aws_iam_role_policy_attachment.ai_gpu_ssm]
}

resource "aws_volume_attachment" "qdrant_data" {
  device_name = "/dev/sdf"
  volume_id   = aws_ebs_volume.qdrant_data.id
  instance_id = aws_instance.ai_gpu.id
}

resource "aws_route53_record" "ai_gpu_private" {
  zone_id = aws_route53_zone.staging_private.zone_id
  name    = "ai-gpu.staging.yeodam.internal"
  type    = "A"
  ttl     = 30
  records = [aws_instance.ai_gpu.private_ip]
}

output "ai_gpu_instance_id" { value = aws_instance.ai_gpu.id }
output "ai_gpu_private_ip" { value = aws_instance.ai_gpu.private_ip }
output "ai_gpu_private_hostname" { value = aws_route53_record.ai_gpu_private.fqdn }
output "ai_gpu_qdrant_volume_id" { value = aws_ebs_volume.qdrant_data.id }
output "ai_api_task_security_group_id" { value = aws_security_group.ai_api_tasks.id }
