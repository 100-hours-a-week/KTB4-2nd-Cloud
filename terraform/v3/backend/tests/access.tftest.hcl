mock_provider "aws" {}
run "disabled" {
  command = plan
  assert {
    condition     = length(aws_iam_role.app) == 0 && length(aws_s3_bucket.app) == 0 && length(aws_eks_pod_identity_association.app) == 0
    error_message = "Disabled must provision nothing."
  }
}
run "minimum_identity_and_storage" {
  command = plan
  variables { enabled = true }
  assert {
    condition     = length(aws_iam_role.app) == 5 && length(aws_eks_pod_identity_association.app) == 5 && length(aws_iam_role_policy.s3) == 4 && length(aws_s3_bucket.app) == 1
    error_message = "Exactly five role identities, four S3 readers and one private bucket."
  }
  assert {
    condition     = aws_s3_bucket.app[0].force_destroy == false && aws_s3_bucket_public_access_block.app[0].block_public_policy && aws_s3_bucket_versioning.app[0].versioning_configuration[0].status == "Enabled"
    error_message = "Private, retained versioned bucket required."
  }
  assert {
    condition     = alltrue([for k, p in aws_iam_role_policy.s3 : alltrue([for s in jsondecode(p.policy).Statement : alltrue([for a in s.Action : startswith(a, "s3:")])])]) && !contains(keys(aws_iam_role_policy.s3), "auth-api")
    error_message = "No GPU/EC2/Secrets permissions or unnecessary auth storage access."
  }
  assert {
    condition     = toset(jsondecode(aws_iam_role_policy.s3["trip-result-worker"].policy).Statement[1].Action) == toset(["s3:GetObject", "s3:GetObjectVersion"])
    error_message = "Result role cannot mutate objects."
  }
  assert {
    condition = alltrue([for role in ["trip-api", "photo-api", "photo-converter"] :
      toset(jsondecode(aws_iam_role_policy.s3[role].policy).Statement[1].Action) == toset(["s3:GetObject", "s3:GetObjectVersion", "s3:PutObject", "s3:PutObjectTagging", "s3:DeleteObject", "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"]) &&
      jsondecode(aws_iam_role_policy.s3[role].policy).Statement[1].Resource == ["arn:aws:s3:::yeodam-v3-foundation-app-data-483175530259-ap-northeast-2/*"]
    ])
    error_message = "Tagged image writes require PutObjectTagging only within the existing V3 bucket."
  }
  assert {
    condition     = alltrue([for k, r in aws_iam_role.app : jsondecode(r.assume_role_policy).Statement[0].Condition.StringEquals["aws:RequestTag/kubernetes-service-account"] == k && jsondecode(r.assume_role_policy).Statement[0].Condition.StringEquals["aws:SourceAccount"] == "483175530259"])
    error_message = "Pod Identity trust must select exact namespace/service account/account/cluster."
  }
  assert {
    condition     = length(aws_s3_bucket_cors_configuration.qa) == 1 && toset(one(aws_s3_bucket_cors_configuration.qa[0].cors_rule).allowed_origins) == toset(["https://v3.yeodam-2gether.com"])
    error_message = "CORS is V3 browser origin only; no wildcard origin."
  }

}
