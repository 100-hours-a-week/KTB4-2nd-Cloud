# Only the V2 staging MySQL host may reach the V1 rehearsal source MySQL.
# The production V1 security group is outside this staging configuration.
resource "aws_vpc_security_group_ingress_rule" "v1_source_mysql_replica" {
  security_group_id            = aws_security_group.v1_source_app.id
  referenced_security_group_id = module.mysql.security_group_id
  description                  = "MySQL replication from V2 staging DB only"
  ip_protocol                  = "tcp"
  from_port                    = 3306
  to_port                      = 3306
}

resource "aws_vpc_security_group_egress_rule" "mysql_v1_source" {
  security_group_id            = module.mysql.security_group_id
  referenced_security_group_id = aws_security_group.v1_source_app.id
  description                  = "MySQL replication to V1 rehearsal source only"
  ip_protocol                  = "tcp"
  from_port                    = 3306
  to_port                      = 3306
}

# Needed only for the later V1 App database cutover rehearsal. This is the
# staging App security group, never the production V1 security group.
resource "aws_vpc_security_group_ingress_rule" "mysql_v1_app" {
  security_group_id            = module.mysql.security_group_id
  referenced_security_group_id = aws_security_group.v1_source_app.id
  description                  = "MySQL from V1 staging App during DB cutover rehearsal"
  ip_protocol                  = "tcp"
  from_port                    = 3306
  to_port                      = 3306
}

# Execute after apply against the V2 MySQL instance. SET PERSIST writes to the
# retained Data EBS and avoids a user_data change on the stateful EC2 instance.
resource "aws_ssm_document" "mysql_replica_server_id" {
  name          = "yeodam-v2-staging-mysql-replica-server-id"
  document_type = "Command"
  content = jsonencode({
    schemaVersion = "2.2"
    description   = "Persist a distinct server_id on the V2 staging MySQL replica candidate"
    mainSteps = [{
      action = "aws:runShellScript"
      name   = "setReplicaServerId"
      inputs = {
        runCommand = [file("${path.module}/files/set-mysql-replica-server-id.sh")]
      }
    }]
  })

  tags = local.common_tags
}
