terraform {
  required_version = ">= 1.10"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

# Separate, temporary local state. Never initialize against the staging backend:
# legacy V1 declarations in that backend still need their own retirement Issue.
provider "aws" {
  region              = "ap-northeast-2"
  profile             = "yeodam-admin"
  allowed_account_ids = ["483175530259"]
}

data "aws_secretsmanager_secret" "redis" {
  name = "yeodam-v2-staging/redis-auth-token"
}

resource "aws_vpc_security_group_ingress_rule" "redis_from_generator" {
  security_group_id            = "sg-0bf0837a3c05ec550"
  referenced_security_group_id = "sg-027e98404a9d634fb"
  ip_protocol                  = "tcp"
  from_port                    = 6379
  to_port                      = 6379
  description                  = "Temporary #474 synthetic session preparation"
  tags                         = { Issue = "474", Lifecycle = "temporary-test" }
}

resource "aws_vpc_security_group_egress_rule" "generator_to_redis" {
  security_group_id            = "sg-027e98404a9d634fb"
  referenced_security_group_id = "sg-0bf0837a3c05ec550"
  ip_protocol                  = "tcp"
  from_port                    = 6379
  to_port                      = 6379
  description                  = "Temporary #474 synthetic session preparation"
  tags                         = { Issue = "474", Lifecycle = "temporary-test" }
}

resource "aws_iam_role_policy" "session_preparation" {
  name = "yeodam-v2-staging-load-generator-session-474"
  role = "yeodam-v2-staging-load-generator"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["ssm:GetParameter"]
        Resource = "arn:aws:ssm:ap-northeast-2:483175530259:parameter/yeodam/v2/staging/app/JWT_SECRET"
      },
      {
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = data.aws_secretsmanager_secret.redis.arn
      }
    ]
  })
}
