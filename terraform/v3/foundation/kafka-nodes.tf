# Initial capacity only: no Kafka Operator, Broker, Topic or application rollout.
# Small nodes are a measured starting point, not validated production capacity.
# This account offers T3a in a/c, but b requires the same-size Intel T3.
locals {
  kafka_instance_types = { for az in keys(local.zones) : az => contains(["ap-northeast-2a", "ap-northeast-2c"], az) ? "t3a.small" : "t3.small" }
}

data "aws_ec2_instance_type_offerings" "kafka" {
  for_each      = var.enable_kafka_nodes ? local.zones : {}
  location_type = "availability-zone"
  filter {
    name   = "location"
    values = [each.key]
  }
  filter {
    name   = "instance-type"
    values = [local.kafka_instance_types[each.key]]
  }
}
resource "aws_launch_template" "kafka" {
  for_each               = var.enable_kafka_nodes ? local.zones : {}
  instance_type          = local.kafka_instance_types[each.key]
  name_prefix            = "${var.cluster_name}-kafka-"
  update_default_version = true

  # Bound charges; exhausted credits throttle CPU rather than incur surplus fees.
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
    tags          = merge(local.tags, { Name = "${var.cluster_name}-kafka", Role = "kafka", Issue = "513" })
  }
  tag_specifications {
    resource_type = "volume"
    tags          = merge(local.tags, { Role = "kafka", Issue = "513" })
  }
}

resource "aws_eks_node_group" "kafka" {
  for_each        = var.enable_kafka_nodes ? local.zones : {}
  cluster_name    = aws_eks_cluster.this.name
  node_group_name = "kafka-${each.key}-small"
  node_role_arn   = aws_iam_role.node.arn
  subnet_ids      = [aws_subnet.private[each.key].id]
  ami_type        = "AL2023_x86_64_STANDARD"
  release_version = var.node_release_version
  version         = var.kubernetes_version
  capacity_type   = "ON_DEMAND"
  labels          = { "yeodam.io/role" = "kafka", "yeodam.io/stage" = "v3" }
  taint {
    key    = "yeodam.io/role"
    value  = "kafka"
    effect = "NO_SCHEDULE"
  }
  launch_template {
    id      = aws_launch_template.kafka[each.key].id
    version = tostring(aws_launch_template.kafka[each.key].latest_version)
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
      condition     = contains(data.aws_ec2_instance_type_offerings.kafka[each.key].instance_types, local.kafka_instance_types[each.key])
      error_message = "The reviewed small instance type is not offered in this AZ; stop before creating an unsupported node group."
    }
  }
  depends_on = [aws_iam_role_policy_attachment.node, aws_eks_addon.cni, aws_route.private, aws_route.public]
}

output "kafka_node_groups" {
  value = { for az, group in aws_eks_node_group.kafka : az => group.node_group_name }
}

# Preserve the original template ID in AZ a; the other AZs need their own
# typed template. The provider omits CreditSpecification without instance_type.
moved {
  from = aws_launch_template.kafka[0]
  to   = aws_launch_template.kafka["ap-northeast-2a"]
}
