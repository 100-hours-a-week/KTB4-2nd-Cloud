mock_provider "aws" {
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::483175530259:role/mocked" }
  }
}

run "namespace_read_boundary" {
  # Mock apply resolves dependent ARNs; no real provider/API calls are made.
  command = apply
  assert {
    condition = alltrue([for ns, policy in aws_iam_role_policy.read : (
      toset(jsondecode(policy.policy).Statement[0].Action) == toset(["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]) &&
      jsondecode(policy.policy).Statement[0].Resource == ["arn:aws:secretsmanager:ap-northeast-2:483175530259:secret:yeodam/v3/foundation/${ns}/*"]
    )])
    error_message = "Readers must only read their own V3 namespace prefix."
  }
  assert {
    condition = (
      length(aws_iam_role.reader) == 5 &&
      toset(jsondecode(aws_iam_role_policy.assume_readers.policy).Statement[0].Action) == toset(["sts:AssumeRole", "sts:TagSession"]) &&
      length(jsondecode(aws_iam_role_policy.assume_readers.policy).Statement[0].Resource) == 5
    )
    error_message = "Core must only assume the five namespace reader roles."
  }
  assert {
    condition = alltrue([for ns, role in aws_iam_role.reader : (
      toset(jsondecode(role.assume_role_policy).Statement[0].Action) == toset(["sts:AssumeRole", "sts:TagSession"]) &&
      jsondecode(role.assume_role_policy).Statement[0].Principal.AWS == aws_iam_role.controller.arn
    )])
    error_message = "Reader trust must accept only the core role, including transitive session tags."
  }
  assert {
    condition = (
      aws_eks_pod_identity_association.controller.namespace == "external-secrets" &&
      aws_eks_pod_identity_association.controller.service_account == "external-secrets" &&
      aws_eks_pod_identity_association.controller.cluster_name == "yeodam-v3-foundation" &&
      jsondecode(aws_iam_role.controller.assume_role_policy).Statement[0].Condition.StringEquals["aws:RequestTag/kubernetes-namespace"] == "external-secrets" &&
      jsondecode(aws_iam_role.controller.assume_role_policy).Statement[0].Condition.StringEquals["aws:RequestTag/kubernetes-service-account"] == "external-secrets" &&
      jsondecode(aws_iam_role.controller.assume_role_policy).Statement[0].Condition.ArnEquals["aws:SourceArn"] == "arn:aws:eks:ap-northeast-2:483175530259:cluster/yeodam-v3-foundation"
    )
    error_message = "Pod Identity must be bound to this cluster's core controller only."
  }
}

run "mysql_reader_opt_in" {
  command = apply
  variables { enable_mysql_reader = true }
  assert {
    condition = (
      length(aws_iam_role.reader) == 6 &&
      length(jsondecode(aws_iam_role_policy.assume_readers.policy).Statement[0].Resource) == 6 &&
      jsondecode(aws_iam_role_policy.read["mysql"].policy).Statement[0].Resource == ["arn:aws:secretsmanager:ap-northeast-2:483175530259:secret:yeodam/v3/foundation/mysql/*"] &&
      toset(jsondecode(aws_iam_role_policy.read["mysql"].policy).Statement[0].Action) == toset(["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"])
    )
    error_message = "Opt-in MySQL reader must read only its V3 namespace prefix."
  }
}

run "redis_reader_preserves_mysql" {
  command = apply
  variables {
    enable_mysql_reader = true
    enable_redis_reader = true
  }
  assert {
    condition = (
      length(aws_iam_role.reader) == 7 &&
      contains(keys(aws_iam_role.reader), "mysql") &&
      length(jsondecode(aws_iam_role_policy.assume_readers.policy).Statement[0].Resource) == 7 &&
      jsondecode(aws_iam_role_policy.read["redis"].policy).Statement[0].Resource == ["arn:aws:secretsmanager:ap-northeast-2:483175530259:secret:yeodam/v3/foundation/redis/*"] &&
      toset(jsondecode(aws_iam_role_policy.read["redis"].policy).Statement[0].Action) == toset(["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"])
    )
    error_message = "Redis reader is namespace-limited and must preserve the existing MySQL reader."
  }
}

run "mongodb_preserves_existing_readers" {
  command = apply
  variables {
    enable_mysql_reader   = true
    enable_redis_reader   = true
    enable_mongodb_reader = true
  }
  assert {
    condition = (
      length(aws_iam_role.reader) == 8 &&
      contains(keys(aws_iam_role.reader), "mysql") && contains(keys(aws_iam_role.reader), "redis") &&
      length(jsondecode(aws_iam_role_policy.assume_readers.policy).Statement[0].Resource) == 8 &&
      jsondecode(aws_iam_role_policy.read["mongodb"].policy).Statement[0].Resource == ["arn:aws:secretsmanager:ap-northeast-2:483175530259:secret:yeodam/v3/foundation/mongodb/*"] &&
      toset(jsondecode(aws_iam_role_policy.read["mongodb"].policy).Statement[0].Action) == toset(["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"])
    )
    error_message = "MongoDB reader must stay in its namespace and retain existing readers."
  }
}
