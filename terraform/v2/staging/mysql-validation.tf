locals {
  mysql_validation_name          = "yeodam-v2-staging-mysql-validation"
  mysql_validation_parameter     = "/yeodam/v2/staging/mysql-validation/ROOT_PASSWORD"
  mysql_validation_parameter_arn = "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter${local.mysql_validation_parameter}"
}

resource "aws_security_group" "mysql_validation" {
  name_prefix = "${local.mysql_validation_name}-"
  description = "Private Backend validation MySQL; SSM administration"
  vpc_id      = module.network.vpc_id
  tags        = merge(local.common_tags, { Name = "${local.mysql_validation_name}-sg", Role = "database" })
}

resource "aws_vpc_security_group_ingress_rule" "mysql_validation_clients" {
  for_each                     = { backend = aws_security_group.backend_tasks.id }
  security_group_id            = aws_security_group.mysql_validation.id
  referenced_security_group_id = each.value
  ip_protocol                  = "tcp"
  from_port                    = 3306
  to_port                      = 3306
  description                  = "${each.key} MySQL access"
}

resource "aws_vpc_security_group_egress_rule" "mysql_validation_clients" {
  for_each                     = { backend = aws_security_group.backend_tasks.id }
  security_group_id            = each.value
  referenced_security_group_id = aws_security_group.mysql_validation.id
  ip_protocol                  = "tcp"
  from_port                    = 3306
  to_port                      = 3306
  description                  = "${each.key} to validation MySQL"
}

resource "aws_vpc_security_group_egress_rule" "mysql_validation_https" {
  security_group_id = aws_security_group.mysql_validation.id
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = "0.0.0.0/0"
  description       = "SSM and HTTPS bootstrap through staging NAT"
}

resource "aws_iam_role" "mysql_validation" {
  name               = local.mysql_validation_name
  assume_role_policy = data.aws_iam_policy_document.v1_source_ec2_assume_role.json
  tags               = merge(local.common_tags, { Role = "database" })
}

resource "aws_iam_role_policy_attachment" "mysql_validation_ssm" {
  role       = aws_iam_role.mysql_validation.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

# Generate the credential on the host, outside Terraform state.
resource "aws_iam_role_policy" "mysql_validation_parameter" {
  role   = aws_iam_role.mysql_validation.id
  name   = "mysql-validation-parameter"
  policy = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Allow", Action = ["ssm:GetParameter", "ssm:PutParameter"], Resource = local.mysql_validation_parameter_arn }] })
}

resource "aws_iam_role_policy" "mysql_validation_app_password" {
  role   = aws_iam_role.mysql_validation.id
  name   = "mysql-validation-app-password-read"
  policy = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Allow", Action = ["ssm:GetParameter"], Resource = "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter${local.mysql_validation_password_parameter}" }] })
}

resource "aws_iam_instance_profile" "mysql_validation" {
  name = local.mysql_validation_name
  role = aws_iam_role.mysql_validation.name
  tags = merge(local.common_tags, { Role = "database" })
}

resource "aws_ebs_volume" "mysql_validation_data" {
  availability_zone = var.app_availability_zone
  size              = 20
  type              = "gp3"
  encrypted         = true
  tags              = merge(local.common_tags, { Name = "${local.mysql_validation_name}-data", Role = "database" })
  lifecycle { prevent_destroy = true }
}

resource "aws_instance" "mysql_validation" {
  ami                         = var.mysql_ami_id
  instance_type               = "t4g.small"
  subnet_id                   = module.network.private_app_subnet_id
  vpc_security_group_ids      = [aws_security_group.mysql_validation.id]
  iam_instance_profile        = aws_iam_instance_profile.mysql_validation.name
  associate_public_ip_address = false
  disable_api_termination     = true
  user_data = templatefile("${path.module}/mysql-validation-bootstrap.sh.tftpl", {
    volume_id     = aws_ebs_volume.mysql_validation_data.id
    region        = var.aws_region
    parameter     = local.mysql_validation_parameter
    app_parameter = local.mysql_validation_password_parameter
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
  tags       = merge(local.common_tags, { Name = local.mysql_validation_name, Role = "database" })
  depends_on = [module.network, aws_iam_role_policy_attachment.mysql_validation_ssm, aws_iam_role_policy.mysql_validation_parameter, aws_iam_role_policy.mysql_validation_app_password]
}

resource "aws_volume_attachment" "mysql_validation_data" {
  device_name = "/dev/sdf"
  volume_id   = aws_ebs_volume.mysql_validation_data.id
  instance_id = aws_instance.mysql_validation.id
}

resource "aws_route53_record" "mysql_validation_private" {
  zone_id = aws_route53_zone.staging_private.zone_id
  name    = "mysql-validation.staging.yeodam.internal"
  type    = "A"
  ttl     = 30
  records = [aws_instance.mysql_validation.private_ip]
}

output "mysql_validation_instance_id" { value = aws_instance.mysql_validation.id }
output "mysql_validation_private_hostname" { value = aws_route53_record.mysql_validation_private.fqdn }
output "mysql_validation_root_parameter_name" { value = local.mysql_validation_parameter }
