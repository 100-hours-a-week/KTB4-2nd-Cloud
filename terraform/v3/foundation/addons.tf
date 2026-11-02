resource "aws_eks_addon" "cni" {
  cluster_name                = aws_eks_cluster.this.name
  addon_name                  = "vpc-cni"
  addon_version               = var.addon_versions["vpc-cni"]
  service_account_role_arn    = aws_iam_role.cni.arn
  resolve_conflicts_on_create = "NONE"
  resolve_conflicts_on_update = "NONE"
  configuration_values = jsonencode({
    enableNetworkPolicy = "true"
    env = {
      ENABLE_PREFIX_DELEGATION      = "false"
      NETWORK_POLICY_ENFORCING_MODE = "standard"
    }
  })
  depends_on = [aws_iam_role_policy_attachment.cni]
}

resource "aws_eks_addon" "node" {
  for_each = toset(["kube-proxy", "coredns", "eks-pod-identity-agent"])

  cluster_name                = aws_eks_cluster.this.name
  addon_name                  = each.value
  addon_version               = var.addon_versions[each.value]
  resolve_conflicts_on_create = "NONE"
  resolve_conflicts_on_update = "NONE"
  depends_on                  = [aws_eks_node_group.system]
}

resource "aws_eks_addon" "ebs" {
  cluster_name                = aws_eks_cluster.this.name
  addon_name                  = "aws-ebs-csi-driver"
  addon_version               = var.addon_versions["aws-ebs-csi-driver"]
  resolve_conflicts_on_create = "NONE"
  resolve_conflicts_on_update = "NONE"

  pod_identity_association {
    role_arn        = aws_iam_role.ebs.arn
    service_account = "ebs-csi-controller-sa"
  }
  depends_on = [aws_eks_addon.node, aws_iam_role_policy_attachment.ebs]
}
