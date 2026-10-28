# Rehearsal connectivity only. Existing services, rules and application settings
# remain owned by staging; these standalone rules are owned by this clone state.
locals {
  runtime_sgs = {
    kafka     = "sg-0fedec4408cea45a3"
    redis     = "sg-0bf0837a3c05ec550"
    generator = "sg-027e98404a9d634fb"
    e2e       = "sg-09e540f61f897c08d"
  }
  ingress455 = {
    source_https = { destination = aws_security_group.clone["source"].id, source = local.runtime_sgs.generator, port = 443 }
    source_cdc   = { destination = aws_security_group.clone["source"].id, source = local.runtime_sgs.kafka, port = 3306 }
    target_cdc   = { destination = aws_security_group.clone["target"].id, source = local.runtime_sgs.kafka, port = 3306 }
    target_e2e   = { destination = aws_security_group.clone["target"].id, source = local.runtime_sgs.e2e, port = 3306 }
    kafka_source = { destination = local.runtime_sgs.kafka, source = aws_security_group.clone["source"].id, port = 9092 }
    redis_source = { destination = local.runtime_sgs.redis, source = aws_security_group.clone["source"].id, port = 6379 }
  }
  egress455 = {
    source_kafka = { source = aws_security_group.clone["source"].id, destination = local.runtime_sgs.kafka, port = 9092 }
    source_redis = { source = aws_security_group.clone["source"].id, destination = local.runtime_sgs.redis, port = 6379 }
    kafka_source = { source = local.runtime_sgs.kafka, destination = aws_security_group.clone["source"].id, port = 3306 }
    kafka_target = { source = local.runtime_sgs.kafka, destination = aws_security_group.clone["target"].id, port = 3306 }
    e2e_target   = { source = local.runtime_sgs.e2e, destination = aws_security_group.clone["target"].id, port = 3306 }
  }
}
resource "aws_vpc_security_group_ingress_rule" "runtime455" {
  for_each                     = local.ingress455
  security_group_id            = each.value.destination
  referenced_security_group_id = each.value.source
  ip_protocol                  = "tcp"
  from_port                    = each.value.port
  to_port                      = each.value.port
  description                  = "Issue455 clone rehearsal ${each.key}"
}
resource "aws_vpc_security_group_egress_rule" "runtime455" {
  for_each                     = local.egress455
  security_group_id            = each.value.source
  referenced_security_group_id = each.value.destination
  ip_protocol                  = "tcp"
  from_port                    = each.value.port
  to_port                      = each.value.port
  description                  = "Issue455 clone rehearsal ${each.key}"
}
resource "aws_iam_role_policy" "runtime455_backup" {
  for_each = local.hosts
  role     = aws_iam_role.clone[each.key].id
  name     = "migration455-scoped-backups"
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject"], Resource = ["arn:aws:s3:::yeodam-v2-staging-mysql-backup-483175530259-ap-northeast-2/migration/performance455/*"] },
    { Effect = "Allow", Action = ["s3:GetObject"], Resource = ["arn:aws:s3:::yeodam-v2-staging-mysql-backup-483175530259-ap-northeast-2/migration/rehearsal-reset/56f1a134-7867-42d6-84df-1c19f030916e/b391-target.sql.gz"] }
  ] })
}
resource "aws_iam_role_policy" "runtime455_auth" {
  role = aws_iam_role.clone["source"].id
  name = "migration455-existing-staging-auth-read"
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["ssm:GetParameter"], Resource = [for path in [
      "v1-source/app/JWT_SECRET", "v1-source/app/JWT_ISSUER",
      "migration/MYSQL_TARGET_PASSWORD", "migration/TARGET_CA_PEM", "cdc-test/SINK_PASSWORD"
    ] : "arn:aws:ssm:ap-northeast-2:483175530259:parameter/yeodam/v2/staging/${path}"] },
    { Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = ["arn:aws:secretsmanager:ap-northeast-2:483175530259:secret:yeodam-v2-staging/redis-auth-token-??????"] }
  ] })
}
