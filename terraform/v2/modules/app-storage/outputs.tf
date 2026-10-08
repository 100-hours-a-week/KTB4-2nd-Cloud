output "bucket_name" {
  description = "Name of the environment-specific photo bucket"
  value       = aws_s3_bucket.app_data.id
}

output "bucket_arn" {
  description = "ARN of the environment-specific photo bucket"
  value       = aws_s3_bucket.app_data.arn
}
