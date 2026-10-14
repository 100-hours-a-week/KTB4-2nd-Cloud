# Retain previous broker logs as evidence; do not recreate the deleted cluster.
resource "aws_cloudwatch_log_group" "msk_brokers" {
  name              = "/yeodam/v2/staging/msk/brokers"
  retention_in_days = 7
  tags              = local.common_tags
}
