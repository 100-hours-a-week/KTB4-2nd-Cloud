variable "name_prefix" { type = string }
variable "vpc_id" { type = string }
variable "private_subnet_id" { type = string }
variable "availability_zone" { type = string }
variable "backend_security_group_id" { type = string }
variable "ami_id" { type = string }
variable "instance_type" { type = string }
variable "data_volume_size_gib" { type = number }
variable "aws_region" { type = string }
variable "tags" { type = map(string) }
variable "mysql_image" { type = string }
variable "backup_bucket_name" { type = string }
variable "backup_bucket_arn" { type = string }

variable "initial_database" {
  description = "Optional empty schema created on first initialization; never imports existing data"
  type        = string
  default     = null
  validation {
    condition     = var.initial_database == null ? true : can(regex("^[a-z][a-z0-9_]{0,63}$", var.initial_database))
    error_message = "The initial schema must be a simple lowercase SQL identifier."
  }
}

variable "replication_filter_schema" {
  description = "Existing staging replication filter; null leaves new production instances unfiltered"
  type        = string
  default     = "yeodam_v1_source"
  validation {
    condition     = var.replication_filter_schema == null ? true : can(regex("^[a-z][a-z0-9_]{0,63}$", var.replication_filter_schema))
    error_message = "The replication schema must be a simple lowercase SQL identifier."
  }
}

variable "backup_metric_namespace" {
  type    = string
  default = "Yeodam/V2/Staging/MySQL"
}

variable "additional_mysql_arguments" {
  description = "Additional fixed server settings; shell metacharacters are forbidden"
  type        = list(string)
  default     = []
  validation {
    condition     = alltrue([for arg in var.additional_mysql_arguments : can(regex("^--[a-z][a-z0-9-]*=[A-Za-z0-9_.-]+$", arg))])
    error_message = "MySQL arguments must be literal --name=value settings."
  }
}

variable "use_https_package_mirrors" {
  description = "Use the canonical Ubuntu ARM HTTPS mirror on new production hosts"
  type        = bool
  default     = false
}
