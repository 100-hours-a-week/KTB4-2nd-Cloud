locals {
  plg_name = "yeodam-v2-staging-plg"
}

resource "aws_security_group" "plg" {
  name_prefix = "${local.plg_name}-"
  description = "Private PLG collector; Grafana is reachable only through SSM port forwarding"
  vpc_id      = module.network.vpc_id
  tags        = merge(local.common_tags, { Name = "${local.plg_name}-sg", Role = "observability" })
}

resource "aws_vpc_security_group_ingress_rule" "plg_loki_from_v1" {
  security_group_id            = aws_security_group.plg.id
  referenced_security_group_id = aws_security_group.v1_source_app.id
  description                  = "V1 App Alloy to Loki only"
  ip_protocol                  = "tcp"
  from_port                    = 3100
  to_port                      = 3100
}

resource "aws_vpc_security_group_ingress_rule" "plg_prometheus_from_v1" {
  security_group_id            = aws_security_group.plg.id
  referenced_security_group_id = aws_security_group.v1_source_app.id
  description                  = "V1 App Alloy remote write only"
  ip_protocol                  = "tcp"
  from_port                    = 9090
  to_port                      = 9090
}

resource "aws_vpc_security_group_egress_rule" "plg_https" {
  security_group_id = aws_security_group.plg.id
  description       = "SSM, registry, package and AWS APIs through staging NAT"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_iam_role" "plg" {
  name               = local.plg_name
  assume_role_policy = data.aws_iam_policy_document.v1_source_ec2_assume_role.json
  tags               = local.common_tags
}

resource "aws_iam_role_policy_attachment" "plg_ssm" {
  role       = aws_iam_role.plg.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

data "aws_iam_policy_document" "plg" {
  statement {
    sid       = "ReadOwnGrafanaPassword"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.plg_admin.arn]
  }

  statement {
    sid       = "ReadStagingBackendLogs"
    actions   = ["logs:StartQuery", "logs:StopQuery", "logs:GetQueryResults", "logs:GetLogGroupFields", "logs:GetLogEvents"]
    resources = [aws_cloudwatch_log_group.v1_source_backend.arn, module.ecs_foundation.backend_log_group_arn]
  }

  statement {
    sid       = "DiscoverStagingLogGroups"
    actions   = ["logs:DescribeLogGroups"]
    resources = ["*"]
  }

  statement {
    sid       = "ReadStagingMetrics"
    actions   = ["cloudwatch:ListMetrics", "cloudwatch:GetMetricData"]
    resources = ["*"]
  }

  statement {
    sid       = "ReadStagingReleaseBundle"
    actions   = ["s3:GetObject"]
    resources = ["${module.v1_source_storage.bucket_arn}/rehearsal-release/*"]
  }
}

resource "aws_iam_role_policy" "plg" {
  name   = "${local.plg_name}-read"
  role   = aws_iam_role.plg.id
  policy = data.aws_iam_policy_document.plg.json
}

resource "aws_iam_instance_profile" "plg" {
  name = local.plg_name
  role = aws_iam_role.plg.name
  tags = local.common_tags
}

# A version is created outside Terraform so the password never enters state.
resource "aws_secretsmanager_secret" "plg_admin" {
  name                    = "yeodam/v2/staging/plg/grafana-admin-password"
  recovery_window_in_days = 7
  tags                    = merge(local.common_tags, { Role = "observability" })
}

resource "aws_ebs_volume" "plg_data" {
  availability_zone = var.app_availability_zone
  size              = var.plg_data_gib
  type              = "gp3"
  encrypted         = true
  tags              = merge(local.common_tags, { Name = "${local.plg_name}-data", Role = "observability" })

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_instance" "plg" {
  ami                         = data.aws_ami.v1_source_ubuntu.id
  instance_type               = var.plg_instance_type
  subnet_id                   = module.network.private_app_subnet_id
  vpc_security_group_ids      = [aws_security_group.plg.id]
  iam_instance_profile        = aws_iam_instance_profile.plg.name
  associate_public_ip_address = false
  monitoring                  = true
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

  tags = merge(local.common_tags, { Name = local.plg_name, Role = "observability" })

  depends_on = [module.network, aws_iam_role_policy_attachment.plg_ssm, aws_iam_role_policy.plg]
}

resource "aws_volume_attachment" "plg_data" {
  device_name = "/dev/sdf"
  volume_id   = aws_ebs_volume.plg_data.id
  instance_id = aws_instance.plg.id
}

resource "aws_route53_record" "plg_private" {
  zone_id = aws_route53_zone.staging_private.zone_id
  name    = "plg.staging.yeodam.internal"
  type    = "A"
  ttl     = 30
  records = [aws_instance.plg.private_ip]
}
