variable "aws_region" {
  description = "AWS Region of the retained shared GPU network"
  type        = string
  default     = "ap-northeast-2"

  validation {
    condition     = var.aws_region == "ap-northeast-2"
    error_message = "V2 staging must be deployed in ap-northeast-2."
  }
}
variable "vpc_cidr" {
  description = "Existing shared GPU VPC CIDR; preserve 10.30.0.0/16"
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
  description = "Existing shared GPU and NAT availability zone"
  type        = string
  default     = "ap-northeast-2a"
}
