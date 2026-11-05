locals {
  events = {
    health    = { source = ["aws.health"], "detail-type" = ["AWS Health Event"] }
    spot      = { source = ["aws.ec2"], "detail-type" = ["EC2 Spot Instance Interruption Warning"] }
    rebalance = { source = ["aws.ec2"], "detail-type" = ["EC2 Instance Rebalance Recommendation"] }
    state     = { source = ["aws.ec2"], "detail-type" = ["EC2 Instance State-change Notification"] }
  }
}
resource "aws_sqs_queue" "interruption" {
  name                      = local.name
  message_retention_seconds = 300
  sqs_managed_sse_enabled   = true
}
resource "aws_cloudwatch_event_rule" "interruption" {
  for_each      = local.events
  name          = "${local.name}-${each.key}"
  event_pattern = jsonencode(merge(each.value, { account = ["483175530259"], region = ["ap-northeast-2"] }))
}
resource "aws_cloudwatch_event_target" "interruption" {
  for_each  = local.events
  rule      = aws_cloudwatch_event_rule.interruption[each.key].name
  target_id = "KarpenterInterruptionQueue"
  arn       = aws_sqs_queue.interruption.arn
}
resource "aws_sqs_queue_policy" "interruption" {
  queue_url = aws_sqs_queue.interruption.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AllowSelectedRules"
        Effect    = "Allow"
        Principal = { Service = "events.amazonaws.com" }
        Action    = "sqs:SendMessage"
        Resource  = aws_sqs_queue.interruption.arn
        Condition = {
          StringEquals = { "aws:SourceAccount" = "483175530259" }
          ArnEquals    = { "aws:SourceArn" = [for rule in aws_cloudwatch_event_rule.interruption : rule.arn] }
        }
      },
      { Sid = "DenyHTTP", Effect = "Deny", Principal = "*", Action = "sqs:*", Resource = aws_sqs_queue.interruption.arn, Condition = { Bool = { "aws:SecureTransport" = "false" } } }
    ]
  })
}
