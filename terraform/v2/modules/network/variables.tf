variable "name_prefix" {
  description = "Prefix for V2 environment resource names"
  type        = string
}

variable "aws_region" {
  description = "AWS Region used for the S3 gateway endpoint"
  type        = string
}

variable "vpc_cidr" {
  description = "IPv4 CIDR for this environment's VPC"
  type        = string
}

variable "app_availability_zone" {
  description = "Availability Zone for the private App subnet and NAT gateway"
  type        = string
}

variable "alb_secondary_availability_zone" {
  description = "Second Availability Zone reserved for the public ALB subnet"
  type        = string
}

variable "tags" {
  description = "Common tags applied to network resources"
  type        = map(string)
}
