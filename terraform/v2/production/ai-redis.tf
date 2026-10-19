locals {
  ai_redis_name          = "${local.name_prefix}-ai-redis"
  ai_redis_parameter     = "/yeodam/v2/production/ai/REDIS_URL"
  ai_redis_parameter_arn = "arn:aws:ssm:ap-northeast-2:${data.aws_caller_identity.current.account_id}:parameter${local.ai_redis_parameter}"
}

data "aws_ami" "ai_redis" {
  owners = ["099720109477"]
  filter {
    name   = "image-id"
    values = ["ami-0ccbfe1123f2d682d"]
  }
  filter {
    name   = "architecture"
    values = ["arm64"]
  }
}

resource "aws_security_group" "ai_redis" {
  name_prefix = "${local.ai_redis_name}-"
  description = "Private AI job state Redis; SSM administration"
  vpc_id      = data.aws_vpc.production.id
  tags        = merge(local.common_tags, { Name = "${local.ai_redis_name}-sg", Role = "ai" })
}

resource "aws_vpc_security_group_ingress_rule" "ai_redis_clients" {
  for_each                     = { api = aws_security_group.ai_kafka_clients.id }
  security_group_id            = aws_security_group.ai_redis.id
  referenced_security_group_id = each.value
  ip_protocol                  = "tcp"
  from_port                    = 6379
  to_port                      = 6379
  description                  = "AI ${each.key} Redis access"
}

resource "aws_vpc_security_group_egress_rule" "ai_redis_clients" {
  for_each                     = { api = aws_security_group.ai_kafka_clients.id }
  security_group_id            = each.value
  referenced_security_group_id = aws_security_group.ai_redis.id
  ip_protocol                  = "tcp"
  from_port                    = 6379
  to_port                      = 6379
  description                  = "AI ${each.key} to dedicated Redis"
}

resource "aws_vpc_security_group_egress_rule" "ai_redis_https" {
  security_group_id = aws_security_group.ai_redis.id
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = "0.0.0.0/0"
  description       = "SSM and HTTPS bootstrap through production NAT"
}

resource "aws_iam_role" "ai_redis" {
  name               = local.ai_redis_name
  assume_role_policy = data.aws_iam_policy_document.kafka_assume_role.json
  tags               = merge(local.common_tags, { Role = "ai" })
}

resource "aws_iam_role_policy_attachment" "ai_redis_ssm" {
  role       = aws_iam_role.ai_redis.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

# Generate the credential on the host, outside Terraform state.
resource "aws_iam_role_policy" "ai_redis_parameter" {
  role   = aws_iam_role.ai_redis.id
  name   = "ai-redis-parameter"
  policy = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Allow", Action = ["ssm:GetParameter", "ssm:PutParameter"], Resource = local.ai_redis_parameter_arn }] })
}

resource "aws_iam_instance_profile" "ai_redis" {
  name = local.ai_redis_name
  role = aws_iam_role.ai_redis.name
  tags = merge(local.common_tags, { Role = "ai" })
}

resource "aws_ebs_volume" "ai_redis_data" {
  availability_zone = "ap-northeast-2a"
  size              = 10
  type              = "gp3"
  encrypted         = true
  tags              = merge(local.common_tags, { Name = "${local.ai_redis_name}-data", Role = "ai" })
  lifecycle { prevent_destroy = true }
}

resource "aws_instance" "ai_redis" {
  ami                         = data.aws_ami.ai_redis.id
  instance_type               = "t4g.small"
  subnet_id                   = aws_subnet.private_app.id
  vpc_security_group_ids      = [aws_security_group.ai_redis.id]
  iam_instance_profile        = aws_iam_instance_profile.ai_redis.name
  associate_public_ip_address = false
  disable_api_termination     = true
  user_data = templatefile("${path.module}/ai-redis-bootstrap.sh.tftpl", {
    volume_id = aws_ebs_volume.ai_redis_data.id
    region    = "ap-northeast-2"
    parameter = local.ai_redis_parameter
    compose   = file("${path.module}/../../../deploy/v2-production/ai-redis-compose.yml")
  })
  credit_specification { cpu_credits = "standard" }
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
  lifecycle { ignore_changes = [user_data] }
  tags       = merge(local.common_tags, { Name = local.ai_redis_name, Role = "ai" })
  depends_on = [aws_route.private_internet, aws_route_table_association.private_app, aws_iam_role_policy_attachment.ai_redis_ssm, aws_iam_role_policy.ai_redis_parameter]
}

resource "aws_volume_attachment" "ai_redis_data" {
  device_name = "/dev/sdf"
  volume_id   = aws_ebs_volume.ai_redis_data.id
  instance_id = aws_instance.ai_redis.id
}

resource "aws_route53_record" "ai_redis_private" {
  zone_id = aws_route53_zone.production_private.zone_id
  name    = "ai-redis.production.yeodam.internal"
  type    = "A"
  ttl     = 30
  records = [aws_instance.ai_redis.private_ip]
}

output "ai_redis_instance_id" { value = aws_instance.ai_redis.id }
output "ai_redis_private_hostname" { value = aws_route53_record.ai_redis_private.fqdn }
output "ai_redis_url_parameter_name" { value = local.ai_redis_parameter }
