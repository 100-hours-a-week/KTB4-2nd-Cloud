variable "name_prefix" {
  description = "Prefix for ALB, target group and security group names"
  type        = string
}

variable "vpc_id" {
  description = "VPC containing the ALB and its targets"
  type        = string
}

variable "public_subnet_ids" {
  description = "Two public subnet IDs in different availability zones"
  type        = list(string)
}

variable "private_app_cidr" {
  description = "Private App subnet CIDR allowed as the ALB target destination"
  type        = string
}

variable "domain_name" {
  description = "DNS name secured by the ALB certificate"
  type        = string
}

variable "hosted_zone_id" {
  description = "Public Route 53 hosted zone ID used for ACM DNS validation"
  type        = string
}

variable "tags" {
  description = "Common tags applied to ALB resources"
  type        = map(string)
}
