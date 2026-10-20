# Only the isolated migration Target is added to the existing Connect path.
resource "aws_vpc_security_group_ingress_rule" "mysql_cdc376" {
  security_group_id            = module.mysql.security_group_id
  referenced_security_group_id = aws_security_group.kafka.id
  description                  = "Migration CDC and sink to isolated rehearsal Target"
  ip_protocol                  = "tcp"
  from_port                    = 3306
  to_port                      = 3306
}
resource "aws_vpc_security_group_egress_rule" "kafka_mysql_cdc376" {
  security_group_id            = aws_security_group.kafka.id
  referenced_security_group_id = module.mysql.security_group_id
  description                  = "Migration CDC and sink to isolated rehearsal Target"
  ip_protocol                  = "tcp"
  from_port                    = 3306
  to_port                      = 3306
}
data "aws_iam_policy_document" "mysql_cdc376_credential" {
  statement {
    actions   = ["ssm:GetParameter"]
    resources = ["arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter${local.mysql_cdc_sink_parameter}"]
  }
}
resource "aws_iam_role_policy" "mysql_cdc376_credential" {
  name   = "yeodam-v2-staging-migration-cdc376-credential"
  role   = module.mysql.instance_role_name
  policy = data.aws_iam_policy_document.mysql_cdc376_credential.json
}

resource "aws_iam_role_policy" "source_cdc376_credential" {
  name   = "yeodam-v2-staging-migration-cdc376-credential"
  role   = aws_iam_role.v1_source_app.name
  policy = data.aws_iam_policy_document.mysql_cdc376_credential.json
}
