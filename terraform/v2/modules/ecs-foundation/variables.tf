variable "name_prefix" {
  description = "Prefix for the ECS cluster and execution role"
  type        = string
}

variable "secret_name" {
  description = "Name of the GHCR repository credentials secret"
  type        = string
}

variable "log_retention_days" {
  description = "CloudWatch Logs retention for FE and BE tasks"
  type        = number
}

variable "tags" {
  description = "Common resource tags"
  type        = map(string)
}
