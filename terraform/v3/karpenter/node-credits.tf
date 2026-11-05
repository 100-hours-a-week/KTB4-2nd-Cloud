variable "enable_app_node_credit_bootstrap" {
  description = "Opt in after review; retain true in applied inputs once enabled. No account-wide credit default change."
  type        = bool
  default     = false
}
# Only the dedicated Karpenter Node role. IAM scope permits tagged api/worker
# peers too; bootstrap code targets its own IMDS ID. No self-only IAM claim.
resource "aws_iam_role_policy" "node_credits" {
  count  = var.enable_app_node_credit_bootstrap ? 1 : 0
  name   = "v3-app-node-standard-credits"
  role   = aws_iam_role.node.name
  policy = file("${path.module}/../../../deploy/v3/nodepools/files/node-credit-policy.json")
  lifecycle { prevent_destroy = true }
}
