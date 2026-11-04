output "vpc_id" { value = module.network.vpc_id }
output "private_app_subnet_id" { value = module.network.private_app_subnet_id }
output "nat_gateway_id" { value = module.network.nat_gateway_id }
output "s3_gateway_endpoint_id" { value = module.network.s3_gateway_endpoint_id }
output "mysql_backup_bucket_name" { value = aws_s3_bucket.mysql_backup.id }
output "load_result_s3_prefix" { value = "s3://${aws_s3_bucket.load_test.id}/load-test-results" }
output "v1_source_app_data_bucket_name" { value = module.v1_source_storage.bucket_name }
