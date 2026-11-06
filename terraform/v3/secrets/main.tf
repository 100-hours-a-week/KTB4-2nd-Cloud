# Independent state: never re-use the foundation or V2 backend key.
provider "aws" {
  region              = "ap-northeast-2"
  allowed_account_ids = ["483175530259"]
  default_tags {
    tags = {
      Project     = "yeodam"
      Version     = "v3"
      Environment = "foundation"
      ManagedBy   = "Terraform"
      Issue       = "500"
    }
  }
}

variable "enable_mysql_reader" {
  description = "Opt in to the V3 MySQL namespace reader; no Secret payloads."
  type        = bool
  default     = false
}

variable "enable_redis_reader" {
  description = "Opt in to the V3 App Redis namespace reader; retain enabled MySQL inputs."
  type        = bool
  default     = false
}

variable "enable_mongodb_reader" {
  description = "Opt in to MongoDB reader; retain both already-applied MySQL and Redis readers."
  type        = bool
  default     = false
}

variable "enable_kafka_reader" {
  description = "Opt in to Kafka's exact V3 CDC secret reader; never grant MySQL bootstrap access."
  type        = bool
  default     = false
}

variable "enable_ai_storage_readers" {
  description = "Opt in to AI Redis and Qdrant bootstrap readers; retain all already-applied reader flags."
  type        = bool
  default     = false
}

locals {
  cluster_name = "yeodam-v3-foundation"
  namespaces   = setunion(toset(["frontend", "auth", "trip", "photo", "ai"]), var.enable_mysql_reader ? toset(["mysql"]) : toset([]), var.enable_redis_reader ? toset(["redis"]) : toset([]), var.enable_mongodb_reader ? toset(["mongodb"]) : toset([]), var.enable_kafka_reader ? toset(["kafka"]) : toset([]), var.enable_ai_storage_readers ? toset(["ai-redis", "qdrant"]) : toset([]))
  secret_arns = {
    for ns in local.namespaces : ns => (ns == "kafka" ? "arn:aws:secretsmanager:ap-northeast-2:483175530259:secret:yeodam/v3/foundation/mysql/cdc-??????" : "arn:aws:secretsmanager:ap-northeast-2:483175530259:secret:yeodam/v3/foundation/${ns}/*")
  }
}

resource "aws_iam_role" "controller" {
  name = "${local.cluster_name}-eso-controller"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "pods.eks.amazonaws.com" }
      Action    = ["sts:AssumeRole", "sts:TagSession"]
      Condition = {
        StringEquals = {
          "aws:SourceAccount"                         = "483175530259"
          "aws:RequestTag/kubernetes-namespace"       = "external-secrets"
          "aws:RequestTag/kubernetes-service-account" = "external-secrets"
        }
        ArnEquals = {
          "aws:SourceArn" = "arn:aws:eks:ap-northeast-2:483175530259:cluster/${local.cluster_name}"
        }
      }
    }]
  })
}

resource "aws_eks_pod_identity_association" "controller" {
  cluster_name    = local.cluster_name
  namespace       = "external-secrets"
  service_account = "external-secrets"
  role_arn        = aws_iam_role.controller.arn
}

# The controller cannot read Secrets Manager directly. A platform-owned
# namespace SecretStore selects one of these reader roles.
resource "aws_iam_role" "reader" {
  for_each = local.namespaces
  name     = "${local.cluster_name}-eso-${each.key}"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { AWS = aws_iam_role.controller.arn }
      # Pod Identity session tags are transitive across this role hop.
      Action = ["sts:AssumeRole", "sts:TagSession"]
    }]
  })
}

resource "aws_iam_role_policy" "assume_readers" {
  name = "namespace-readers"
  role = aws_iam_role.controller.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["sts:AssumeRole", "sts:TagSession"]
      Resource = [for ns in sort(tolist(local.namespaces)) : aws_iam_role.reader[ns].arn]
    }]
  })
}

resource "aws_iam_role_policy" "read" {
  for_each = local.namespaces
  name     = "namespace-secret-read"
  role     = aws_iam_role.reader[each.key].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
      Resource = [local.secret_arns[each.key]]
    }]
  })
}

# No aws_secretsmanager_secret_version, data source reads or payloads in State.
# This root does not create Secret values, EC2, nodes or VPC endpoints.
output "namespace_reader_roles" {
  value = { for ns, role in aws_iam_role.reader : ns => role.arn }
}

output "source_prefixes" {
  value = { for ns in local.namespaces : ns => "yeodam/v3/foundation/${ns}/" }
}
