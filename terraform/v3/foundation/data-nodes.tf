# Shared storage capacity only. Database Pods/PVCs are separate follow-up work.
# Type is pinned per AZ so CPU credit settings reach EC2 through a typed LT.
locals {
  data_nodes = {
    ap-northeast-2a = { instance_type = "t3a.medium", memory_tier = "4gi" }
    ap-northeast-2c = { instance_type = "t3a.small", memory_tier = "2gi" }
  }
}

data "aws_ec2_instance_type_offerings" "data" {
  for_each      = var.enable_data_nodes ? local.data_nodes : {}
  location_type = "availability-zone"
  filter {
    name   = "location"
    values = [each.key]
  }
  filter {
    name   = "instance-type"
    values = [each.value.instance_type]
  }
}

resource "aws_launch_template" "data" {
  for_each               = var.enable_data_nodes ? local.data_nodes : {}
  name_prefix            = "${var.cluster_name}-data-"
  instance_type          = each.value.instance_type
  update_default_version = true
  credit_specification {
    cpu_credits = "standard"
  }
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }
  block_device_mappings {
    device_name = "/dev/xvda"
    ebs {
      volume_type           = "gp3"
      volume_size           = 20
      encrypted             = true
      delete_on_termination = true
    }
  }
  tag_specifications {
    resource_type = "instance"
    tags          = merge(local.tags, { Name = "${var.cluster_name}-data", Role = "data", Issue = "519" })
  }
  tag_specifications {
    resource_type = "volume"
    tags          = merge(local.tags, { Role = "data", Issue = "519" })
  }
  tags = merge(local.tags, { Role = "data", Issue = "519" })
}

resource "aws_eks_node_group" "data" {
  for_each        = var.enable_data_nodes ? local.data_nodes : {}
  cluster_name    = aws_eks_cluster.this.name
  node_group_name = "data-${each.key}-minimal"
  node_role_arn   = aws_iam_role.node.arn
  subnet_ids      = [aws_subnet.private[each.key].id]
  ami_type        = "AL2023_x86_64_STANDARD"
  release_version = var.node_release_version
  version         = var.kubernetes_version
  capacity_type   = "ON_DEMAND"
  labels = {
    "yeodam.io/role"        = "data"
    "yeodam.io/stage"       = "v3"
    "yeodam.io/memory-tier" = each.value.memory_tier
  }
  taint {
    key    = "yeodam.io/role"
    value  = "data"
    effect = "NO_SCHEDULE"
  }
  launch_template {
    id      = aws_launch_template.data[each.key].id
    version = tostring(aws_launch_template.data[each.key].latest_version)
  }
  scaling_config {
    desired_size = 1
    min_size     = 1
    max_size     = 1
  }
  update_config {
    max_unavailable = 1
  }
  lifecycle {
    precondition {
      condition     = contains(data.aws_ec2_instance_type_offerings.data[each.key].instance_types, each.value.instance_type)
      error_message = "The reviewed storage type is unavailable in this AZ; do not substitute a larger instance."
    }
  }
  tags       = merge(local.tags, { Role = "data", Issue = "519" })
  depends_on = [aws_iam_role_policy_attachment.node, aws_eks_addon.cni, aws_route.private, aws_route.public]
}

output "data_node_groups" {
  value = { for az, group in aws_eks_node_group.data : az => group.node_group_name }
}
