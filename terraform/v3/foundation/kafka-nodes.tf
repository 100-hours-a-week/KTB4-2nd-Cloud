# Initial capacity only: no Kafka Operator, Broker, Topic or application rollout.
# Small nodes are a measured starting point, not validated production capacity.
resource "aws_launch_template" "kafka" {
  count                  = var.enable_kafka_nodes ? 1 : 0
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
  node_group_name = "kafka-${each.key}-t3a-small"
  node_role_arn   = aws_iam_role.node.arn
  subnet_ids      = [aws_subnet.private[each.key].id]
  ami_type        = "AL2023_x86_64_STANDARD"
  release_version = var.node_release_version
  version         = var.kubernetes_version
  capacity_type   = "ON_DEMAND"
  instance_types  = ["t3a.small"]
  labels          = { "yeodam.io/role" = "kafka", "yeodam.io/stage" = "v3" }
  taint {
    key    = "yeodam.io/role"
    value  = "kafka"
    effect = "NO_SCHEDULE"
  }
  launch_template {
    id      = aws_launch_template.kafka[0].id
    version = tostring(aws_launch_template.kafka[0].latest_version)
  }
  scaling_config {
    desired_size = 1
    min_size     = 1
    max_size     = 1
  }
  update_config {
    max_unavailable = 1
  }
  depends_on = [aws_iam_role_policy_attachment.node, aws_eks_addon.cni, aws_route.private, aws_route.public]
}

output "kafka_node_groups" {
  value = { for az, group in aws_eks_node_group.kafka : az => group.node_group_name }
}
