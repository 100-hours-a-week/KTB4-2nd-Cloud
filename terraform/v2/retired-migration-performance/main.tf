# Retirement root for the former #444/#455 clone state, not the staging root.
# Its remaining dedicated IAM/SG resources are removed; AMI/Snapshot/S3 backups
# are outside this state and remain untouched.
terraform {
  required_version = ">= 1.10"
  backend "s3" {
    key          = "yeodam/v2/migration-performance/terraform.tfstate"
    region       = "ap-northeast-2"
    encrypt      = true
    use_lockfile = true
  }
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}
provider "aws" {
  region              = "ap-northeast-2"
  allowed_account_ids = ["483175530259"]
}
