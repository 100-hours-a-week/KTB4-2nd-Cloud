locals {
  kafka_name = "yeodam-v2-staging-kafka"
}

resource "aws_security_group" "kafka" {
  name_prefix = "${local.kafka_name}-"
  description = "Private Kafka host; administration through SSM only"
  vpc_id      = module.network.vpc_id
  tags        = merge(local.common_tags, { Name = "${local.kafka_name}-sg", Role = "kafka" })
}

# Attach this client SG to the future AI API and GPU host. It grants only the
# Kafka path; their HTTPS, Redis and embedding rules belong to their own SGs.
resource "aws_security_group" "ai_kafka_clients" {
  name_prefix = "yeodam-v2-staging-ai-kafka-"
  description = "AI clients permitted to reach staging Kafka"
  vpc_id      = module.network.vpc_id
  tags        = merge(local.common_tags, { Name = "yeodam-v2-staging-ai-kafka-sg", Role = "ai" })
}

resource "aws_vpc_security_group_ingress_rule" "kafka_backend" {
  security_group_id            = aws_security_group.kafka.id
  referenced_security_group_id = aws_security_group.backend_tasks.id
  description                  = "Backend Kafka clients"
  ip_protocol                  = "tcp"
  from_port                    = 9092
  to_port                      = 9092
}

resource "aws_vpc_security_group_egress_rule" "backend_kafka" {
  security_group_id            = aws_security_group.backend_tasks.id
  referenced_security_group_id = aws_security_group.kafka.id
  description                  = "Backend to private Kafka"
  ip_protocol                  = "tcp"
  from_port                    = 9092
  to_port                      = 9092
}

resource "aws_vpc_security_group_ingress_rule" "kafka_ai" {
  security_group_id            = aws_security_group.kafka.id
  referenced_security_group_id = aws_security_group.ai_kafka_clients.id
  description                  = "AI Kafka clients"
  ip_protocol                  = "tcp"
  from_port                    = 9092
  to_port                      = 9092
}

resource "aws_vpc_security_group_egress_rule" "ai_kafka" {
  security_group_id            = aws_security_group.ai_kafka_clients.id
  referenced_security_group_id = aws_security_group.kafka.id
  description                  = "AI to private Kafka"
  ip_protocol                  = "tcp"
  from_port                    = 9092
  to_port                      = 9092
}

resource "aws_vpc_security_group_egress_rule" "kafka_https" {
  security_group_id = aws_security_group.kafka.id
  description       = "SSM and HTTPS package/image downloads through NAT"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_iam_role" "kafka" {
  name               = local.kafka_name
  assume_role_policy = data.aws_iam_policy_document.v1_source_ec2_assume_role.json
  tags               = merge(local.common_tags, { Role = "kafka" })
}

resource "aws_iam_role_policy_attachment" "kafka_ssm" {
  role       = aws_iam_role.kafka.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "kafka" {
  name = local.kafka_name
  role = aws_iam_role.kafka.name
  tags = merge(local.common_tags, { Role = "kafka" })
}

resource "aws_ebs_volume" "kafka_data" {
  availability_zone = var.app_availability_zone
  size              = 100
  type              = "gp3"
  encrypted         = true
  tags              = merge(local.common_tags, { Name = "${local.kafka_name}-data", Role = "kafka" })

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_instance" "kafka" {
  ami                         = data.aws_ami.v1_source_ubuntu.id
  instance_type               = "t3.medium"
  subnet_id                   = module.network.private_app_subnet_id
  vpc_security_group_ids      = [aws_security_group.kafka.id]
  iam_instance_profile        = aws_iam_instance_profile.kafka.name
  associate_public_ip_address = false
  disable_api_termination     = true
  user_data                   = templatefile("${path.module}/kafka-bootstrap.sh.tftpl", { volume_id = aws_ebs_volume.kafka_data.id })

  credit_specification {
    cpu_credits = "standard"
  }

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  root_block_device {
    volume_type           = "gp3"
    volume_size           = 20
    encrypted             = true
    delete_on_termination = true
  }

  tags       = merge(local.common_tags, { Name = local.kafka_name, Role = "kafka" })
  depends_on = [module.network, aws_iam_role_policy_attachment.kafka_ssm]
}

resource "aws_volume_attachment" "kafka_data" {
  device_name = "/dev/sdf"
  volume_id   = aws_ebs_volume.kafka_data.id
  instance_id = aws_instance.kafka.id
}

resource "aws_route53_record" "kafka_private" {
  zone_id = aws_route53_zone.staging_private.zone_id
  name    = "kafka.staging.yeodam.internal"
  type    = "A"
  ttl     = 30
  records = [aws_instance.kafka.private_ip]
}
