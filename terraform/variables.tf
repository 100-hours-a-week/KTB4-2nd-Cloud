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
