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

variable "redis_node_type" {
  description = "Initial staging Redis node size; revisit after auth-state load measurements"
  type        = string
  default     = "cache.t4g.micro"
}

variable "v1_source_ami_id" {
  description = "Reviewed Ubuntu 24.04 x86_64 AMI for the V1 rehearsal App and Worker"
  type        = string
  default     = "ami-086a43496cb46286c"
}

variable "v1_source_app_instance_type" {
  description = "V1 rehearsal App size; production parity is t3.medium"
  type        = string
  default     = "t3.medium"
}

variable "v1_source_worker_instance_type" {
  description = "V1 rehearsal AI Worker size; stop outside rehearsal windows"
  type        = string
  default     = "c7i.xlarge"
}

variable "v1_source_mysql_data_gib" {
  description = "Retained MySQL data volume for the V1 rehearsal source"
  type        = number
  default     = 20

  validation {
    condition     = var.v1_source_mysql_data_gib >= 10
    error_message = "The V1 rehearsal MySQL volume must be at least 10 GiB."
  }
}

variable "plg_instance_type" {
  description = "Private staging PLG host size; measure memory under rehearsal load"
  type        = string
  default     = "t3.medium"
}

variable "plg_data_gib" {
  description = "Retained EBS capacity for the staging PLG data stores"
  type        = number
  default     = 40

  validation {
    condition     = var.plg_data_gib >= 20
    error_message = "PLG data volume must have at least 20 GiB."
  }
}
