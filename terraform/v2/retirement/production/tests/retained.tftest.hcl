mock_provider "aws" {
  mock_data "aws_ssm_document" {
    defaults = {
      content = "{\"schemaVersion\":\"2.2\",\"mainSteps\":[]}"
    }
  }
}
run "retain_shared_gpu_dns_and_backup" {
  command = plan
  assert {
    condition     = one(aws_route53_zone.production_private.vpc).vpc_id == "vpc-0d84786e134e32933"
    error_message = "The bootstrap GPU DNS association must survive retirement."
  }
  assert {
    condition     = aws_s3_bucket.mysql_backup.bucket == "yeodam-v2-production-mysql-backup-483175530259-ap-northeast-2"
    error_message = "Never replace the actual migration backup bucket."
  }
  assert {
    condition     = length(aws_route53_zone_association.legacy_production) == 0
    error_message = "The obsolete VPC link is absent by default; adoption is separate."
  }
}
