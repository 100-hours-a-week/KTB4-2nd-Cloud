locals {
  github_oidc_provider_arn = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:oidc-provider/token.actions.githubusercontent.com"
  staging_deploy_subject   = "repo:100-hours-a-week@167328634/KTB4-2nd-Cloud@1344655631:environment:staging"
  frontend_service_arn     = "arn:aws:ecs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:service/yeodam-v2-staging-app/yeodam-v2-staging-frontend"
}

data "aws_iam_policy_document" "frontend_staging_deploy_assume" {
  statement {
    sid     = "AllowCloudStagingEnvironment"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [local.github_oidc_provider_arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = [local.staging_deploy_subject]
    }
  }
}

resource "aws_iam_role" "staging_release_deploy" {
  name                 = "yeodam-v2-staging-release-deploy"
  description          = "Promote a verified V2 staging image combination"
  assume_role_policy   = data.aws_iam_policy_document.frontend_staging_deploy_assume.json
  max_session_duration = 7200
  tags                 = local.common_tags
}
