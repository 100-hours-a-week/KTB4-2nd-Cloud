provider "aws" {
  region              = var.aws_region
  allowed_account_ids = ["483175530259"]

  default_tags {
    tags = local.common_tags
  }
}
