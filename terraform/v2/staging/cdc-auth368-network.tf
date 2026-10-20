# The migration auth consumer runs only on the isolated V1 rehearsal Source.
# Redis access was granted for the A auth projection; Kafka is scoped to 9092.
resource "aws_vpc_security_group_ingress_rule" "kafka_v1_source_auth368" {
  security_group_id            = aws_security_group.kafka.id
  referenced_security_group_id = aws_security_group.v1_source_app.id
  description                  = "V1 rehearsal auth CDC consumer to staging Kafka"
  ip_protocol                  = "tcp"
  from_port                    = 9092
  to_port                      = 9092
}
