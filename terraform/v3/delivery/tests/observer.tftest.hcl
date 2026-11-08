mock_provider "aws" {
  mock_data "aws_iam_openid_connect_provider" {
    defaults = { arn = "arn:aws:iam::483175530259:oidc-provider/token.actions.githubusercontent.com" }
  }
  mock_data "aws_eks_cluster" {
    defaults = { endpoint = "https://AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA.gr7.ap-northeast-2.eks.amazonaws.com", vpc_config = [{ vpc_id = "vpc-0ecc3132f3b2170e9" }] }
  }
}
run "disabled" {
  command = plan
  assert {
    condition     = length(aws_iam_role.status) == 0 && length(aws_ssm_document.status) == 0 && length(aws_eks_access_entry.status) == 0
    error_message = "Default disabled must create nothing."
  }
}
run "observer" {
  command = plan
  variables {
    enabled             = true
    system_instance_ids = ["i-11111111111111111"]
  }
  assert {
    condition     = jsondecode(aws_ssm_document.status[0].content).sessionType == "Port" && jsondecode(aws_ssm_document.status[0].content).properties.portNumber == "443"
    error_message = "Fixed API TLS tunnel only."
  }
  assert {
    condition     = aws_eks_access_entry.status[0].kubernetes_groups == toset(["yeodam-v3-delivery-observer"])
    error_message = "No admin access policy."
  }
  assert {
    condition     = length(jsondecode(aws_iam_role_policy.status[0].policy).Statement) == 4
    error_message = "Only observer/tunnel actions."
  }
}

run "immutable_repository_subject" {
  command = plan
  variables {
    enabled             = true
    system_instance_ids = ["i-11111111111111111"]
  }
  assert {
    condition     = jsondecode(aws_iam_role.status[0].assume_role_policy).Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:sub"] == "repo:100-hours-a-week@167328634/KTB4-2nd-Cloud@1344655631:environment:v3-production"
    error_message = "Use the repository's real immutable subject and only the approved Environment."
  }
}
