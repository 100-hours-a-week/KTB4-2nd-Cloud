mock_provider "aws" {}
run "disabled" {
  command = plan
  assert {
    condition     = length(aws_iam_role.app) == 0 && length(aws_eks_pod_identity_association.app) == 0
    error_message = "Disabled creates nothing."
  }
}
run "minimal_read_only_identities" {
  command = plan
  variables { enabled = true }
  assert {
    condition     = length(aws_iam_role.app) == 2 && length(aws_eks_pod_identity_association.app) == 2 && length(aws_iam_role_policy.read) == 2
    error_message = "Only two roles, two policies and two associations."
  }
  assert {
    condition     = alltrue([for k, p in aws_iam_role_policy.read : jsondecode(p.policy).Statement[0].Action == ["s3:GetObject"]]) && length(jsondecode(aws_iam_role_policy.read["ai-api"].policy).Statement[0].Resource) == 2 && length(jsondecode(aws_iam_role_policy.read["ai-runner"].policy).Statement[0].Resource) == 3
    error_message = "Read-only V3 images; exact reference only for runner."
  }
  assert {
    condition     = alltrue([for k, r in aws_iam_role.app : jsondecode(r.assume_role_policy).Statement[0].Condition.StringEquals["aws:RequestTag/kubernetes-service-account"] == k && jsondecode(r.assume_role_policy).Statement[0].Condition.StringEquals["aws:RequestTag/kubernetes-namespace"] == "ai"])
    error_message = "Exact ai namespace and service account trust."
  }
}
