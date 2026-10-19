# Keep the existing GPU owned by staging. Production owns only the new peering,
# standalone routes/rules, private alias and production Qdrant runtime document.
data "aws_instance" "shared_gpu" {
  instance_id = "i-05e1ad9dd96dba4ae"
  lifecycle {
    postcondition {
      condition = (
        self.subnet_id == "subnet-0f5d19ddb40fbc262" &&
        self.private_ip == "10.30.10.162" && self.instance_type == "g6.xlarge"
      )
      error_message = "Review the shared GPU identity before changing cross-environment access."
    }
  }
}

data "aws_subnet" "shared_gpu" {
  id = data.aws_instance.shared_gpu.subnet_id
  lifecycle {
    postcondition {
      condition     = self.vpc_id == "vpc-0d84786e134e32933" && self.cidr_block == "10.30.10.0/24"
      error_message = "Expected the existing GPU private subnet and staging VPC."
    }
  }
}

data "aws_vpc" "shared_gpu" {
  id = data.aws_subnet.shared_gpu.vpc_id
  lifecycle {
    postcondition {
      condition     = self.cidr_block == "10.30.0.0/16"
      error_message = "Expected the existing staging VPC; do not overlap production CIDR."
    }
  }
}

data "aws_security_group" "shared_gpu" {
  id = "sg-0a80da95d51c138fe"
  lifecycle {
    postcondition {
      condition     = self.vpc_id == data.aws_vpc.shared_gpu.id && self.tags["Name"] == "yeodam-v2-staging-ai-gpu-sg"
      error_message = "Only the existing GPU-specific security group may receive production rules."
    }
  }
}

data "aws_route_table" "shared_gpu" {
  subnet_id = data.aws_instance.shared_gpu.subnet_id
  lifecycle {
    postcondition {
      condition     = self.id == "rtb-0fc40e73348be69f0"
      error_message = "Review the staging GPU return route table before applying."
    }
  }
}

resource "aws_vpc_peering_connection" "shared_gpu" {
  vpc_id      = data.aws_vpc.production.id
  peer_vpc_id = data.aws_vpc.shared_gpu.id
  auto_accept = true
  tags        = { Name = "${local.name_prefix}-shared-gpu", Role = "ai" }
}

resource "aws_route" "production_to_shared_gpu" {
  route_table_id            = aws_route_table.private_app.id
  destination_cidr_block    = "${data.aws_instance.shared_gpu.private_ip}/32"
  vpc_peering_connection_id = aws_vpc_peering_connection.shared_gpu.id
}

resource "aws_route" "shared_gpu_to_production" {
  route_table_id            = data.aws_route_table.shared_gpu.id
  destination_cidr_block    = aws_subnet.private_app.cidr_block
  vpc_peering_connection_id = aws_vpc_peering_connection.shared_gpu.id
}

resource "aws_vpc_security_group_ingress_rule" "shared_gpu_production" {
  for_each                     = { embedding = 8000, qdrant = 6335 }
  security_group_id            = data.aws_security_group.shared_gpu.id
  referenced_security_group_id = aws_security_group.ai_kafka_clients.id
  ip_protocol                  = "tcp"
  from_port                    = each.value
  to_port                      = each.value
  description                  = "Production AI to shared ${each.key}; staging rules remain separate"
  depends_on                   = [aws_vpc_peering_connection.shared_gpu]
}

resource "aws_vpc_security_group_egress_rule" "production_shared_gpu" {
  for_each                     = { embedding = 8000, qdrant = 6335 }
  security_group_id            = aws_security_group.ai_kafka_clients.id
  referenced_security_group_id = data.aws_security_group.shared_gpu.id
  ip_protocol                  = "tcp"
  from_port                    = each.value
  to_port                      = each.value
  description                  = "Production AI to shared ${each.key} over peering"
  depends_on                   = [aws_vpc_peering_connection.shared_gpu]
}

resource "aws_route53_record" "shared_gpu_production" {
  zone_id = aws_route53_zone.production_private.zone_id
  name    = "ai-gpu.production.yeodam.internal"
  type    = "A"
  ttl     = 30
  records = [data.aws_instance.shared_gpu.private_ip]
}

resource "aws_ssm_document" "production_qdrant_runtime" {
  name          = "${local.name_prefix}-qdrant-runtime"
  document_type = "Command"
  content = jsonencode({
    schemaVersion = "2.2"
    description   = "Add isolated production Qdrant on shared GPU; preserve the three staging containers"
    mainSteps = [{
      action = "aws:runShellScript"
      name   = "installProductionQdrant"
      inputs = {
        timeoutSeconds = "600"
        runCommand = [
          "export EXPECTED_INSTANCE_ID='${data.aws_instance.shared_gpu.id}'",
          "export EXPECTED_DATA_VOLUME='vol-05d2497eaeabb57d3'",
          "export EXPECTED_BIND_IP='${data.aws_instance.shared_gpu.private_ip}'",
          "export PRODUCTION_QDRANT_COMPOSE_B64='${filebase64("${path.module}/../../../deploy/v2-production/qdrant/compose.yml")}'",
          "bash -s <<'YEODAM_PRODUCTION_QDRANT'\n${file("${path.module}/../../../scripts/install-v2-production-qdrant.sh")}\nYEODAM_PRODUCTION_QDRANT",
        ]
      }
    }]
  })
  tags = local.common_tags
}

output "shared_gpu_instance_id" { value = data.aws_instance.shared_gpu.id }
output "shared_gpu_peering_id" { value = aws_vpc_peering_connection.shared_gpu.id }
output "production_embedding_base_url" { value = "http://${aws_route53_record.shared_gpu_production.fqdn}:8000/v1" }
output "production_qdrant_url" { value = "http://${aws_route53_record.shared_gpu_production.fqdn}:6335" }
output "production_qdrant_runtime_ssm_document_name" { value = aws_ssm_document.production_qdrant_runtime.name }
