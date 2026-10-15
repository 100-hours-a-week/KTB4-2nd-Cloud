resource "aws_ssm_document" "kafka_runtime" {
  name          = "yeodam-v2-staging-kafka-runtime"
  document_type = "Command"
  content = jsonencode({
    schemaVersion = "2.2"
    description   = "Install staging Kafka and Connect; reconcile agreed topics without registering DB connectors"
    mainSteps = [{
      action = "aws:runShellScript"
      name   = "installKafkaRuntime"
      inputs = {
        timeoutSeconds = "1200"
        runCommand = [
          "export KAFKA_COMPOSE_B64='${filebase64("${path.module}/../../../deploy/v2-staging/kafka/compose.yml")}'",
          "export KAFKA_TOPICS_B64='${filebase64("${path.module}/../../../deploy/v2-staging/kafka/topics.sh")}'",
          "printf '%s' '${filebase64("${path.module}/../../../scripts/install-v2-staging-kafka.sh")}' | base64 --decode > /opt/yeodam-kafka/install-runtime.sh",
          "bash /opt/yeodam-kafka/install-runtime.sh",
        ]
      }
    }]
  })
  tags = local.common_tags
}

output "kafka_runtime_ssm_document_name" { value = aws_ssm_document.kafka_runtime.name }
