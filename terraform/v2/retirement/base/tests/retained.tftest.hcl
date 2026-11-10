mock_provider "aws" {}
run "retain_shared_identity_and_reference_objects" {
  command = plan
  assert {
    condition     = aws_s3_bucket.app_data.bucket == "yeodam-v1-app-data-483175530259-ap-northeast-2"
    error_message = "Shared AI reference and original photo objects must retain their bucket."
  }
  assert {
    condition     = aws_iam_openid_connect_provider.github_actions.url == "https://token.actions.githubusercontent.com"
    error_message = "All approved V3 delivery roles still need this shared OIDC provider."
  }
}
