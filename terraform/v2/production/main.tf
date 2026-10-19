locals {
  name_prefix = "yeodam-v2-production"
  common_tags = {
    Project     = "yeodam"
    Version     = "v2"
    Environment = "production"
    ManagedBy   = "Terraform"
  }
}

data "aws_caller_identity" "current" {}

# V1 owns these resources in terraform/. Never import them into this state.
data "aws_vpc" "production" {
  id = "vpc-08b0e3afa68321309"

  lifecycle {
    postcondition {
      condition     = self.cidr_block == "10.20.0.0/16" && self.enable_dns_support && self.enable_dns_hostnames
      error_message = "Expected the existing production VPC with DNS enabled."
    }
  }
}

data "aws_subnet" "v1_public" {
  id = "subnet-06a38130b599506b9"

  lifecycle {
    postcondition {
      condition = (
        self.vpc_id == data.aws_vpc.production.id &&
        self.cidr_block == "10.20.1.0/24" &&
        self.availability_zone == "ap-northeast-2a"
      )
      error_message = "The V1 public subnet must remain in the expected production VPC/AZ."
    }
  }
}

data "aws_internet_gateway" "production" {
  filter {
    name   = "attachment.vpc-id"
    values = [data.aws_vpc.production.id]
  }
}
