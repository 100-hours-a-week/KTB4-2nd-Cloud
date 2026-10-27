terraform {
  required_version = ">= 1.11.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
  backend "s3" {
    key          = "yeodam/v2/migration-performance/terraform.tfstate"
    region       = "ap-northeast-2"
    encrypt      = true
    use_lockfile = true
  }
}

provider "aws" {
  region              = "ap-northeast-2"
  allowed_account_ids = ["483175530259"]
  default_tags {
    tags = {
      Project     = "yeodam"
      Environment = "migration-performance"
      ManagedBy   = "Terraform"
      Issue       = "444"
    }
  }
}
