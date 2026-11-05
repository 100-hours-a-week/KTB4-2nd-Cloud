data "aws_caller_identity" "current" {}

resource "aws_security_group" "ai_kafka_clients" {
  name_prefix = "yeodam-v2-staging-ai-kafka-"
  description = "AI clients permitted to reach staging Kafka"
  vpc_id      = module.network.vpc_id
  tags        = merge(local.common_tags, { Name = "yeodam-v2-staging-ai-kafka-sg", Role = "ai" })
}


resource "aws_route53_zone" "staging_private" {
  name = "staging.yeodam.internal"

  vpc {
    vpc_id = module.network.vpc_id
  }

  tags = local.common_tags
}
