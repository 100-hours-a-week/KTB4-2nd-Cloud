variable "bucket_name" {
  description = "Environment-specific S3 bucket name for application photos"
  type        = string
}

variable "browser_origin" {
  description = "Allowed browser origin for presigned PUT uploads"
  type        = string
}

variable "tags" {
  description = "Common resource tags"
  type        = map(string)
}
