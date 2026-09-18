variable "aws_region" {
  description = "AWS Region used for the Yeodam V1 infrastructure"
  type        = string
  default     = "ap-northeast-2"

  validation {
    condition     = var.aws_region == "ap-northeast-2"
    error_message = "Yeodam V1 resources must be deployed in ap-northeast-2."
  }
}

variable "project_name" {
  description = "Project name used for resource names and tags"
  type        = string
  default     = "yeodam"

  validation {
    condition     = can(regex("^[a-z0-9-]+$", var.project_name))
    error_message = "project_name must contain only lowercase letters, numbers, and hyphens."
  }
}

variable "deployment_version" {
  description = "V1 identifier used for resource names and tags"
  type        = string
  default     = "v1"

  validation {
    condition     = can(regex("^v[0-9]+$", var.deployment_version))
    error_message = "deployment_version must use the form v1, v2, and so on."
  }
}

variable "vpc_cidr" {
  description = "IPv4 CIDR block for the Yeodam V1 VPC"
  type        = string
  default     = "10.20.0.0/16"

  validation {
    condition     = var.vpc_cidr == "10.20.0.0/16"
    error_message = "Yeodam V1 VPC CIDR must be 10.20.0.0/16."
  }
}

variable "public_subnet_cidr" {
  description = "IPv4 CIDR block for the Yeodam V1 public subnet"
  type        = string
  default     = "10.20.1.0/24"

  validation {
    condition     = var.public_subnet_cidr == "10.20.1.0/24"
    error_message = "Yeodam V1 public subnet CIDR must be 10.20.1.0/24."
  }
}

variable "availability_zone" {
  description = "Availability Zone used for the Yeodam V1 public subnet"
  type        = string
  default     = "ap-northeast-2a"

  validation {
    condition     = var.availability_zone == "ap-northeast-2a"
    error_message = "Yeodam V1 public subnet must be deployed in ap-northeast-2a."
  }
}

variable "ubuntu_ami_id" {
  description = "Pinned Canonical Ubuntu Server 24.04 LTS AMI ID for V1 EC2 instances"
  type        = string
  default     = "ami-086a43496cb46286c"

  validation {
    condition     = can(regex("^ami-[0-9a-f]{17}$", var.ubuntu_ami_id))
    error_message = "ubuntu_ami_id must be a valid EC2 AMI ID."
  }
}

variable "app_instance_type" {
  description = "EC2 instance type for the Yeodam V1 App server"
  type        = string
  default     = "t3.medium"

  validation {
    condition     = var.app_instance_type == "t3.medium"
    error_message = "The Yeodam V1 App EC2 instance type must be t3.medium."
  }
}

variable "app_root_volume_size" {
  description = "Root EBS volume size in GiB for the V1 App EC2 instance"
  type        = number
  default     = 30

  validation {
    condition     = var.app_root_volume_size == 30
    error_message = "The V1 App EC2 root volume size must be 30 GiB."
  }
}

variable "mysql_data_volume_size" {
  description = "MySQL data EBS volume size in GiB"
  type        = number
  default     = 10

  validation {
    condition     = var.mysql_data_volume_size == 10
    error_message = "The V1 MySQL data volume size must be 10 GiB."
  }
}
