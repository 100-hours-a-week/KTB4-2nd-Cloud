provider "aws" {
  region              = var.aws_region
  allowed_account_ids = [var.expected_account_id]

  default_tags {
    tags = local.tags
  }
}

locals {
  tags = {
    Project     = "yeodam"
    Version     = "v3"
    Environment = "foundation"
    ManagedBy   = "Terraform"
    Issue       = "481"
  }
  # AZ list order is the existing subnet CIDR allocation; only append new AZs.
  zones     = { for index, az in var.availability_zones : az => index }
  nat_zones = { for az, index in local.zones : az => index if !contains(keys(var.nat_gateway_az_overrides), az) }
}

data "aws_caller_identity" "current" {}

check "admin_accounts" {
  assert {
    condition     = alltrue([for arn in var.admin_role_arns : split(":", arn)[4] == var.expected_account_id])
    error_message = "Every initial operator role must belong to the reviewed account."
  }
}
