resource "aws_cloudwatch_log_group" "cluster" {
  name              = "/aws/eks/${var.cluster_name}/cluster"
  retention_in_days = 14
}

resource "aws_eks_cluster" "this" {
  name                          = var.cluster_name
  version                       = var.kubernetes_version
  role_arn                      = aws_iam_role.cluster.arn
  bootstrap_self_managed_addons = false
  enabled_cluster_log_types     = ["api", "audit", "authenticator", "controllerManager", "scheduler"]

  access_config {
    authentication_mode                         = "API"
    bootstrap_cluster_creator_admin_permissions = false
  }

  vpc_config {
    subnet_ids              = [for subnet in aws_subnet.private : subnet.id]
    endpoint_private_access = true
    endpoint_public_access  = true
    public_access_cidrs     = var.api_public_access_cidrs
  }

  kubernetes_network_config {
    ip_family         = "ipv4"
    service_ipv4_cidr = "172.20.0.0/16"
  }

  # Do not silently opt into extended-support charges.
  upgrade_policy {
    support_type = "STANDARD"
  }

  lifecycle {
    precondition {
      condition     = alltrue([for arn in var.admin_role_arns : split(":", arn)[4] == var.expected_account_id])
      error_message = "Operator roles must be in the expected account."
    }
  }

  depends_on = [aws_iam_role_policy_attachment.cluster, aws_cloudwatch_log_group.cluster]
}

resource "aws_eks_access_entry" "admin" {
  for_each      = var.admin_role_arns
  cluster_name  = aws_eks_cluster.this.name
  principal_arn = each.value
  type          = "STANDARD"
}

resource "aws_eks_access_policy_association" "admin" {
  for_each      = var.admin_role_arns
  cluster_name  = aws_eks_cluster.this.name
  principal_arn = aws_eks_access_entry.admin[each.key].principal_arn
  policy_arn    = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"

  access_scope {
    type = "cluster"
  }
}

resource "aws_launch_template" "system" {
  name_prefix            = "${var.cluster_name}-system-"
  update_default_version = true

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  block_device_mappings {
    device_name = "/dev/xvda"
    ebs {
      volume_type           = "gp3"
      volume_size           = 30
      encrypted             = true
      delete_on_termination = true
    }
  }

  tag_specifications {
    resource_type = "instance"
    tags          = merge(local.tags, { Name = "${var.cluster_name}-system" })
  }
  tag_specifications {
    resource_type = "volume"
    tags          = local.tags
  }
}

# Separate single-AZ groups guarantee one system node in each AZ. No app/GPU
# pools or Karpenter permissions are introduced in this foundation issue.
resource "aws_eks_node_group" "system" {
  for_each        = local.zones
  cluster_name    = aws_eks_cluster.this.name
  node_group_name = "system-${each.key}"
  node_role_arn   = aws_iam_role.node.arn
  subnet_ids      = [aws_subnet.private[each.key].id]
  ami_type        = "AL2023_x86_64_STANDARD"
  release_version = var.node_release_version
  version         = var.kubernetes_version
  capacity_type   = "ON_DEMAND"
  instance_types  = [var.node_instance_type]
  labels          = { "yeodam.io/role" = "system" }

  launch_template {
    id      = aws_launch_template.system.id
    version = aws_launch_template.system.latest_version
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
