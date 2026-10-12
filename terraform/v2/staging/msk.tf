resource "aws_security_group" "msk_brokers" {
  name_prefix = "yeodam-v2-staging-msk-"
  description = "Private MSK brokers for V2 staging"
  vpc_id      = module.network.vpc_id

  tags = merge(local.common_tags, { Name = "yeodam-v2-staging-msk-sg" })
}

# Broker internals must be able to communicate across the two AZs.
resource "aws_vpc_security_group_ingress_rule" "msk_broker_internal" {
  security_group_id            = aws_security_group.msk_brokers.id
  referenced_security_group_id = aws_security_group.msk_brokers.id
  description                  = "MSK broker communication within the cluster"
  ip_protocol                  = "-1"
}

resource "aws_vpc_security_group_egress_rule" "msk_broker_internal" {
  security_group_id            = aws_security_group.msk_brokers.id
  referenced_security_group_id = aws_security_group.msk_brokers.id
  description                  = "MSK broker communication within the cluster"
  ip_protocol                  = "-1"
}

resource "aws_vpc_security_group_ingress_rule" "msk_backend_iam" {
  security_group_id            = aws_security_group.msk_brokers.id
  referenced_security_group_id = aws_security_group.backend_tasks.id
  description                  = "Backend IAM-authenticated Kafka clients"
  ip_protocol                  = "tcp"
  from_port                    = 9098
  to_port                      = 9098
}

resource "aws_vpc_security_group_egress_rule" "backend_msk_iam" {
  security_group_id            = aws_security_group.backend_tasks.id
  referenced_security_group_id = aws_security_group.msk_brokers.id
  description                  = "MSK IAM-authenticated Kafka clients"
  ip_protocol                  = "tcp"
  from_port                    = 9098
  to_port                      = 9098
}

resource "aws_cloudwatch_log_group" "msk_brokers" {
  name              = "/yeodam/v2/staging/msk/brokers"
  retention_in_days = 7

  tags = local.common_tags
}

resource "aws_msk_cluster" "staging" {
  cluster_name           = "yeodam-v2-staging"
  kafka_version          = var.msk_kafka_version
  number_of_broker_nodes = 2
  enhanced_monitoring    = "DEFAULT"
  storage_mode           = "LOCAL"

  broker_node_group_info {
    client_subnets  = module.network.private_msk_subnet_ids
    instance_type   = var.msk_broker_instance_type
    security_groups = [aws_security_group.msk_brokers.id]

    connectivity_info {
      public_access {
        type = "DISABLED"
      }
    }

    storage_info {
      ebs_storage_info {
        volume_size = var.msk_storage_gib
      }
    }
  }

  client_authentication {
    sasl {
      iam = true
    }
    unauthenticated = false
  }

  encryption_info {
    encryption_in_transit {
      client_broker = "TLS"
      in_cluster    = true
    }
  }

  logging_info {
    broker_logs {
      cloudwatch_logs {
        enabled   = true
        log_group = aws_cloudwatch_log_group.msk_brokers.name
      }
    }
  }

  depends_on = [
    aws_vpc_security_group_ingress_rule.msk_broker_internal,
    aws_vpc_security_group_egress_rule.msk_broker_internal,
  ]

  tags = local.common_tags
}

# Topic and consumer-group grants follow the BE/AI message contract.
data "aws_iam_policy_document" "backend_msk_connect" {
  statement {
    sid       = "ConnectToStagingMsk"
    actions   = ["kafka-cluster:Connect"]
    resources = [aws_msk_cluster.staging.arn]
  }
}

resource "aws_iam_role_policy" "backend_msk_connect" {
  name   = "yeodam-v2-staging-backend-msk-connect"
  role   = aws_iam_role.backend_task.id
  policy = data.aws_iam_policy_document.backend_msk_connect.json
}
