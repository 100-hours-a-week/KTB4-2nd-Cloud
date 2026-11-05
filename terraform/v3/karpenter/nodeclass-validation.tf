# Karpenter1.15 validates a NodeClass using a synthetic NodeClaim with an
# empty nodepool tag. Preserve api/worker lifecycle policy; add only this class.
variable "enable_app_nodeclass_validation" {
  description = "Opt in to the named app NodeClass's empty-pool-tag validation path. Retain enabled credit inputs."
  type        = bool
  default     = false
}
resource "aws_iam_policy" "nodeclass_validation" {
  count  = var.enable_app_nodeclass_validation ? 1 : 0
  name   = "${local.name}-nodeclass-validation"
  policy = file("${path.module}/../../../deploy/v3/nodepools/files/nodeclass-validation-policy.json")
  lifecycle {
    prevent_destroy = true
    precondition {
      condition     = var.enable_app_node_credit_bootstrap
      error_message = "Retain the already-applied node-credit policy when enabling app class validation."
    }
  }
}
resource "aws_iam_role_policy_attachment" "nodeclass_validation" {
  count      = var.enable_app_nodeclass_validation ? 1 : 0
  role       = aws_iam_role.controller.name
  policy_arn = aws_iam_policy.nodeclass_validation[0].arn
  lifecycle { prevent_destroy = true }
}

# The pinned controller also checks a synthesized legacy profile on finalization,
# even with spec.instanceProfile. It is absent; allow lookup, never profile mutation.
