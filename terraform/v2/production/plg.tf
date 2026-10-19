locals {
  plg_name = "${local.name_prefix}-plg"
}
resource "aws_security_group" "plg" {
  name_prefix = "${local.plg_name}-"
  description = "Private production observers; Grafana only via SSM"
  vpc_id      = data.aws_vpc.production.id
  tags        = { Name = "${local.plg_name}-sg", Role = "observability" }
}
resource "aws_vpc_security_group_egress_rule" "plg_https" {
  security_group_id = aws_security_group.plg.id
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = "0.0.0.0/0"
  description       = "SSM, AWS read APIs, registries and alert delivery through NAT"
}
resource "aws_iam_role" "plg" {
  name               = local.plg_name
  assume_role_policy = data.aws_iam_policy_document.kafka_assume_role.json
}
resource "aws_iam_role_policy_attachment" "plg_ssm" {
  role       = aws_iam_role.plg.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}
resource "aws_iam_instance_profile" "plg" {
  name = local.plg_name
  role = aws_iam_role.plg.name
}
resource "aws_secretsmanager_secret" "plg_admin" {
  name                    = "yeodam/v2/production/plg/grafana-admin-password"
  recovery_window_in_days = 7
}
resource "aws_ebs_volume" "plg_data" {
  availability_zone = "ap-northeast-2a"
  size              = 40
  type              = "gp3"
  encrypted         = true
  tags              = { Name = "${local.plg_name}-data" }
  lifecycle { prevent_destroy = true }
}
resource "aws_instance" "plg" {
  ami                         = data.aws_ami.kafka_ubuntu.id
  instance_type               = "t3.medium"
  subnet_id                   = aws_subnet.private_app.id
  vpc_security_group_ids      = [aws_security_group.plg.id]
  iam_instance_profile        = aws_iam_instance_profile.plg.name
  associate_public_ip_address = false
  disable_api_termination     = true
  user_data                   = templatefile("${path.module}/plg-bootstrap.sh.tftpl", { volume_id = aws_ebs_volume.plg_data.id })
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 2
  }
  root_block_device {
    volume_type           = "gp3"
    volume_size           = 20
    encrypted             = true
    delete_on_termination = true
  }
  credit_specification { cpu_credits = "standard" }
  tags       = { Name = local.plg_name, Role = "observability" }
  depends_on = [aws_route.private_internet, aws_route_table_association.private_app, aws_iam_role_policy_attachment.plg_ssm, aws_iam_role_policy.plg]
}
resource "aws_volume_attachment" "plg_data" {
  device_name = "/dev/sdf"
  volume_id   = aws_ebs_volume.plg_data.id
  instance_id = aws_instance.plg.id
}
resource "aws_route53_record" "plg_private" {
  zone_id = aws_route53_zone.production_private.zone_id
  name    = "plg.production.yeodam.internal"
  type    = "A"
  ttl     = 30
  records = [aws_instance.plg.private_ip]
}
resource "aws_iam_role_policy" "plg" {
  name = "${local.plg_name}-read"
  role = aws_iam_role.plg.name
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = aws_secretsmanager_secret.plg_admin.arn },
    { Effect = "Allow", Action = ["ssm:GetParameter"], Resource = ["arn:aws:ssm:ap-northeast-2:${data.aws_caller_identity.current.account_id}:parameter/yeodam/v2/production/ai/API_KEY", "arn:aws:ssm:ap-northeast-2:${data.aws_caller_identity.current.account_id}:parameter/yeodam/v1/observability/discord-webhook-url"] },
    { Effect = "Allow", Action = ["logs:StartQuery", "logs:StopQuery", "logs:GetQueryResults", "logs:GetLogGroupFields", "logs:GetLogEvents"], Resource = [trimsuffix(module.ecs_foundation.backend_log_group_arn, ":*"), "arn:aws:logs:ap-northeast-2:${data.aws_caller_identity.current.account_id}:log-group:${module.ecs_foundation.frontend_log_group_name}", trimsuffix(aws_cloudwatch_log_group.ai_api.arn, ":*")] },
    { Effect = "Allow", Action = ["logs:DescribeLogGroups", "cloudwatch:ListMetrics", "cloudwatch:GetMetricData", "ec2:DescribeInstances"], Resource = "*" },
    { Effect = "Allow", Action = ["ecs:ListTasks"], Resource = "*", Condition = { ArnEquals = { "ecs:cluster" = module.ecs_foundation.cluster_arn } } },
    { Effect = "Allow", Action = ["ecs:DescribeTasks"], Resource = "arn:aws:ecs:ap-northeast-2:${data.aws_caller_identity.current.account_id}:task/yeodam-v2-production-app/*" },
    { Effect = "Allow", Action = ["cloudwatch:PutMetricData"], Resource = "*", Condition = { StringEquals = { "cloudwatch:namespace" = "Yeodam/V2/Production", "aws:RequestedRegion" = "ap-northeast-2" } } }
  ] })
}
output "plg_instance_id" { value = aws_instance.plg.id }
