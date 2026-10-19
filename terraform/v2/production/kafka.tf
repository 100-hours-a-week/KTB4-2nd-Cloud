locals {
  kafka_name = "${local.name_prefix}-kafka"
}

data "aws_ami" "kafka_ubuntu" {
  owners      = ["099720109477"]
  most_recent = false
  filter {
    name   = "image-id"
    values = ["ami-086a43496cb46286c"]
  }
  filter {
    name   = "architecture"
    values = ["x86_64"]
  }
}

resource "aws_security_group" "kafka" {
  name_prefix = "${local.kafka_name}-"
  description = "Private production Kafka; administration through SSM only"
  vpc_id      = data.aws_vpc.production.id
  tags        = { Name = "${local.kafka_name}-sg", Role = "kafka" }
}

resource "aws_security_group" "ai_kafka_clients" {
  name_prefix = "${local.name_prefix}-ai-kafka-"
  description = "Future production AI Kafka clients; no GPU attachment in this change"
  vpc_id      = data.aws_vpc.production.id
  tags        = { Name = "${local.name_prefix}-ai-kafka-sg", Role = "ai" }
}

resource "aws_vpc_security_group_ingress_rule" "kafka_clients" {
  for_each                     = { backend = aws_security_group.backend_tasks.id, ai = aws_security_group.ai_kafka_clients.id }
  security_group_id            = aws_security_group.kafka.id
  referenced_security_group_id = each.value
  from_port                    = 9092
  to_port                      = 9092
  ip_protocol                  = "tcp"
  description                  = "Production ${each.key} Kafka clients"
}

resource "aws_vpc_security_group_egress_rule" "kafka_clients" {
  for_each                     = { backend = aws_security_group.backend_tasks.id, ai = aws_security_group.ai_kafka_clients.id }
  security_group_id            = each.value
  referenced_security_group_id = aws_security_group.kafka.id
  from_port                    = 9092
  to_port                      = 9092
  ip_protocol                  = "tcp"
  description                  = "Production ${each.key} to private Kafka"
}

resource "aws_vpc_security_group_egress_rule" "kafka_https" {
  security_group_id = aws_security_group.kafka.id
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"
  description       = "SSM and HTTPS image/package downloads through production NAT"
}

resource "aws_vpc_security_group_egress_rule" "ai_kafka_https" {
  security_group_id = aws_security_group.ai_kafka_clients.id
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"
  description       = "HTTPS image/dependency downloads for private AI clients"
}

data "aws_iam_policy_document" "kafka_assume_role" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "kafka" {
  name               = local.kafka_name
  assume_role_policy = data.aws_iam_policy_document.kafka_assume_role.json
  tags               = { Role = "kafka" }
}

resource "aws_iam_role_policy_attachment" "kafka_ssm" {
  role       = aws_iam_role.kafka.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "kafka" {
  name = local.kafka_name
  role = aws_iam_role.kafka.name
  tags = { Role = "kafka" }
}

resource "aws_ebs_volume" "kafka_data" {
  availability_zone = "ap-northeast-2a"
  size              = 100
  type              = "gp3"
  encrypted         = true
  tags              = { Name = "${local.kafka_name}-data", Role = "kafka" }
  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_instance" "kafka" {
  ami                         = data.aws_ami.kafka_ubuntu.id
  instance_type               = "t3.medium"
  subnet_id                   = aws_subnet.private_app.id
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
  tags = { Name = local.kafka_name, Role = "kafka" }
  lifecycle {
    ignore_changes = [user_data]
  }
  depends_on = [aws_route.private_internet, aws_route_table_association.private_app, aws_iam_role_policy_attachment.kafka_ssm]
}

resource "aws_volume_attachment" "kafka_data" {
  device_name = "/dev/sdf"
  volume_id   = aws_ebs_volume.kafka_data.id
  instance_id = aws_instance.kafka.id
}

resource "aws_route53_record" "kafka_private" {
  zone_id = aws_route53_zone.production_private.zone_id
  name    = "kafka.production.yeodam.internal"
  type    = "A"
  ttl     = 30
  records = [aws_instance.kafka.private_ip]
}
