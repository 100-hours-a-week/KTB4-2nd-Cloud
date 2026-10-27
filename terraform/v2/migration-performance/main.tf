locals {
  vpc_id = "vpc-0d84786e134e32933"
  hosts = {
    source = {
      instance_type = "t3.medium"
      architecture  = "x86_64"
      subnet_id     = "subnet-0a1454391e0f12f22"
      data_gib      = 20
      public_ip     = true
      monitoring    = true
      metadata_hops = 2
    }
    target = {
      instance_type = "t4g.medium"
      architecture  = "arm64"
      subnet_id     = "subnet-0f5d19ddb40fbc262"
      data_gib      = 120
      public_ip     = false
      monitoring    = true
      metadata_hops = 2
    }
  }
}

data "aws_ami" "clone" {
  for_each = local.hosts
  owners   = ["self"]
  filter {
    name   = "image-id"
    values = [var.images[each.key].ami_id]
  }
  filter {
    name   = "architecture"
    values = [each.value.architecture]
  }
  filter {
    name   = "tag:MigrationCloneIssue"
    values = ["444"]
  }
}

data "aws_ebs_snapshot" "data" {
  for_each    = local.hosts
  owners      = ["self"]
  most_recent = false
  filter {
    name   = "snapshot-id"
    values = [var.images[each.key].data_snapshot_id]
  }
}

resource "aws_security_group" "clone" {
  for_each    = local.hosts
  name_prefix = "yeodam-migration-perf-${each.key}-"
  description = "Quarantined migration clone; no application ingress or shared Kafka/Redis access"
  vpc_id      = local.vpc_id
  tags        = { Name = "yeodam-migration-perf-${each.key}" }
}

# Initial quarantine intentionally permits no private database, Kafka, Redis,
# observability or application traffic. AWS SSM/S3/registry HTTPS only.
resource "aws_vpc_security_group_egress_rule" "https" {
  for_each          = local.hosts
  security_group_id = aws_security_group.clone[each.key].id
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = "0.0.0.0/0"
  description       = "HTTPS bootstrap and SSM; no shared data-plane ports"
}

resource "aws_iam_role" "clone" {
  for_each = local.hosts
  name     = "yeodam-migration-perf-${each.key}"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "ssm" {
  for_each   = local.hosts
  role       = aws_iam_role.clone[each.key].name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy" "read_bootstrap" {
  for_each = local.hosts
  role     = aws_iam_role.clone[each.key].id
  name     = "read-reviewed-migration-bootstrap"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = ["s3:GetObject"]
        Resource = [
          "arn:aws:s3:::yeodam-v2-staging-mysql-backup-483175530259-ap-northeast-2/migration/rehearsal-reset/3502031b-0040-4333-9a8c-9ce63d9a2ce8/b391-target.sql.gz",
          "arn:aws:s3:::yeodam-v2-staging-mysql-backup-483175530259-ap-northeast-2/migration/performance444/*"
        ]
      }
    ]
  })
}

resource "aws_iam_role_policy" "source_registry" {
  role = aws_iam_role.clone["source"].id
  name = "read-pinned-staging-image-credential"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["secretsmanager:GetSecretValue"]
      Resource = ["arn:aws:secretsmanager:ap-northeast-2:483175530259:secret:yeodam/v2/staging/ghcr-credentials-??????"]
    }]
  })
}

resource "aws_iam_instance_profile" "clone" {
  for_each = local.hosts
  name     = "yeodam-migration-perf-${each.key}"
  role     = aws_iam_role.clone[each.key].name
}

resource "aws_instance" "clone" {
  for_each                    = local.hosts
  ami                         = data.aws_ami.clone[each.key].id
  instance_type               = each.value.instance_type
  subnet_id                   = each.value.subnet_id
  vpc_security_group_ids      = [aws_security_group.clone[each.key].id]
  iam_instance_profile        = aws_iam_instance_profile.clone[each.key].name
  associate_public_ip_address = each.value.public_ip
  monitoring                  = each.value.monitoring
  ebs_optimized               = false
  disable_api_termination     = true
  user_data                   = templatefile("${path.module}/quarantine.sh.tftpl", { role = each.key })
  user_data_replace_on_change = true

  credit_specification { cpu_credits = "unlimited" }
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = each.value.metadata_hops
  }
  root_block_device {
    volume_type           = "gp3"
    volume_size           = 30
    iops                  = 3000
    throughput            = 125
    encrypted             = true
    delete_on_termination = true
  }
  ebs_block_device {
    device_name           = "/dev/sdf"
    snapshot_id           = data.aws_ebs_snapshot.data[each.key].id
    volume_type           = "gp3"
    volume_size           = each.value.data_gib
    iops                  = 3000
    throughput            = 125
    encrypted             = true
    delete_on_termination = false
  }
  tags = {
    Name = "yeodam-migration-perf-${each.key}"
    Role = "migration-${each.key}"
  }
  volume_tags = { Role = "migration-${each.key}", Issue = "444" }

  lifecycle {
    prevent_destroy = true
    precondition {
      condition     = data.aws_ebs_snapshot.data[each.key].encrypted && data.aws_ebs_snapshot.data[each.key].volume_size == each.value.data_gib
      error_message = "Clone data snapshot must be encrypted and match the original data volume size."
    }
    precondition {
      condition = anytrue([
        for block in data.aws_ami.clone[each.key].block_device_mappings :
        block.device_name == "/dev/sdf" && try(block.ebs.snapshot_id, "") == var.images[each.key].data_snapshot_id
      ])
      error_message = "Data snapshot must belong to this AMI's /dev/sdf mapping."
    }
  }
  depends_on = [aws_iam_role_policy_attachment.ssm, aws_iam_role_policy.read_bootstrap, aws_iam_role_policy.source_registry]
}

# New-to-new connectivity only; the original Target SG is never attached here.
resource "aws_vpc_security_group_egress_rule" "source_clone_target" {
  security_group_id            = aws_security_group.clone["source"].id
  referenced_security_group_id = aws_security_group.clone["target"].id
  ip_protocol                  = "tcp"
  from_port                    = 3306
  to_port                      = 3306
  description                  = "Source clone to Target clone only"
}

resource "aws_vpc_security_group_ingress_rule" "target_clone_source" {
  security_group_id            = aws_security_group.clone["target"].id
  referenced_security_group_id = aws_security_group.clone["source"].id
  ip_protocol                  = "tcp"
  from_port                    = 3306
  to_port                      = 3306
  description                  = "Target clone from Source clone only"
}
