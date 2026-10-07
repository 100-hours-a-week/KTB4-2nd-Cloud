variable "aws_region" {
  description = "AWS Region for the V2 staging network"
  type        = string
  default     = "ap-northeast-2"

  validation {
    condition     = var.aws_region == "ap-northeast-2"
    error_message = "V2 staging must be deployed in ap-northeast-2."
  }
}

variable "vpc_cidr" {
  description = "Proposed, non-overlapping V2 staging VPC CIDR; verify against account VPCs before apply"
  type        = string
  default     = "10.30.0.0/16"

  validation {
    condition = (
      can(cidrsubnet(var.vpc_cidr, 8, 10)) &&
      try(tonumber(split("/", var.vpc_cidr)[1]), -1) == 16
    )
    error_message = "vpc_cidr must be a valid IPv4 /16 CIDR for the derived /24 subnets."
  }
}

variable "app_availability_zone" {
  description = "Availability Zone for App, MySQL, Redis and the NAT gateway"
  type        = string
  default     = "ap-northeast-2a"
}

variable "alb_secondary_availability_zone" {
  description = "Second Availability Zone reserved for the ALB public subnet"
  type        = string
  default     = "ap-northeast-2c"

  validation {
    condition     = var.alb_secondary_availability_zone != var.app_availability_zone
    error_message = "The two ALB public subnets must be in different availability zones."
  }
}

variable "mysql_ami_id" {
  description = "Reviewed Canonical Ubuntu 24.04 ARM64 AMI for the staging MySQL host"
  type        = string
  default     = "ami-0ccbfe1123f2d682d"
}

variable "mysql_active_private_ip" {
  description = "Validated MySQL private IP to publish in the private zone; null until recovery checks pass"
  type        = string
  default     = null

  validation {
    condition     = var.mysql_active_private_ip == null || can(cidrhost("${var.mysql_active_private_ip}/32", 0))
    error_message = "mysql_active_private_ip must be a valid IPv4 address or null."
  }
}
