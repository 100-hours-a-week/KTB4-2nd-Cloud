provider "aws" {
  region              = "ap-northeast-2"
  allowed_account_ids = ["483175530259"]

  default_tags {
    tags = local.common_tags
  }
}
