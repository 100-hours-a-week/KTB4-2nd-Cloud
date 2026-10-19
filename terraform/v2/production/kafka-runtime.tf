resource "aws_ssm_document" "kafka_runtime" {
  name          = "${local.kafka_name}-runtime"
  document_type = "Command"
  content = jsonencode({
    schemaVersion = "2.2"
    description   = "Install production Kafka/Connect and agreed topics; do not register DB connectors"
    mainSteps = [{
      action = "aws:runShellScript"
      name   = "installProductionKafka"
      inputs = {
        timeoutSeconds = "1200"
        runCommand = [
          "export EXPECTED_INSTANCE_ID='${aws_instance.kafka.id}'",
          "export KAFKA_COMPOSE_B64='${filebase64("${path.module}/../../../deploy/v2-production/kafka/compose.yml")}'",
          "export KAFKA_TOPICS_B64='${filebase64("${path.module}/../../../deploy/v2-production/kafka/topics.sh")}'",
          "bash -s <<'YEODAM_PRODUCTION_KAFKA'\n${file("${path.module}/../../../scripts/install-v2-production-kafka.sh")}\nYEODAM_PRODUCTION_KAFKA",
        ]
      }
    }]
  })
  tags = local.common_tags
}
