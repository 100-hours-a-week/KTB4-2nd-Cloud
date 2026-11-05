mock_provider "aws" {
  mock_resource "aws_iam_policy" { defaults = { arn = "arn:aws:iam::483175530259:policy/mocked" } }
  mock_resource "aws_iam_role" { defaults = { arn = "arn:aws:iam::483175530259:role/mocked-node" } }
  mock_resource "aws_iam_instance_profile" { defaults = { arn = "arn:aws:iam::483175530259:instance-profile/mocked-node" } }
  mock_resource "aws_sqs_queue" { defaults = { arn = "arn:aws:sqs:ap-northeast-2:483175530259:yeodam-v3-foundation-karpenter" } }
  mock_resource "aws_cloudwatch_event_rule" { defaults = { arn = "arn:aws:events:ap-northeast-2:483175530259:rule/mocked-rule" } }
}

run "controller_only_permissions_and_events" {
  command = apply
  assert {
    condition = (
      aws_eks_pod_identity_association.controller.cluster_name == "yeodam-v3-foundation" &&
      aws_eks_pod_identity_association.controller.namespace == "karpenter" &&
      aws_eks_pod_identity_association.controller.service_account == "karpenter" &&
      jsondecode(aws_iam_role.controller.assume_role_policy).Statement[0].Condition.StringEquals["aws:RequestTag/kubernetes-namespace"] == "karpenter" &&
      jsondecode(aws_iam_role.controller.assume_role_policy).Statement[0].Condition.StringEquals["aws:RequestTag/kubernetes-service-account"] == "karpenter" &&
      jsondecode(aws_iam_role.controller.assume_role_policy).Statement[0].Condition.StringEquals["aws:SourceAccount"] == "483175530259" &&
      jsondecode(aws_iam_role.controller.assume_role_policy).Statement[0].Condition.ArnEquals["aws:SourceArn"] == local.cluster_arn
    )
    error_message = "Only this cluster's karpenter ServiceAccount can assume controller identity."
  }
  assert {
    condition = (
      aws_eks_access_entry.node.type == "EC2_LINUX" &&
      aws_eks_access_entry.node.cluster_name == local.cluster_name &&
      aws_eks_access_entry.node.principal_arn == aws_iam_role.node.arn &&
      aws_iam_instance_profile.node.role == aws_iam_role.node.name &&
      toset(keys(aws_iam_role_policy_attachment.node)) == toset(["AmazonEKSWorkerNodePolicy", "AmazonEC2ContainerRegistryPullOnly", "AmazonSSMManagedInstanceCore"])
    )
    error_message = "Dedicated node identity must not receive cluster admin or CNI permissions."
  }
  assert {
    condition = alltrue([for st in jsondecode(aws_iam_policy.lifecycle.policy).Statement : (
      st.Effect == "Allow" && !contains(try(tolist(st.Action), [st.Action]), "ec2:RebootInstances") &&
      !contains(try(tolist(st.Action), [st.Action]), "ec2:ModifyInstanceAttribute")
      )]) && alltrue([for sid in ["AllowScopedEC2LaunchTemplateAccessActions", "AllowScopedEC2InstanceActionsWithTags", "AllowScopedResourceCreationTagging", "AllowScopedResourceTagging", "AllowScopedDeletion"] : (
      length([for st in jsondecode(aws_iam_policy.lifecycle.policy).Statement : st if st.Sid == sid &&
        try(st.Condition.StringEquals["aws:ResourceTag/karpenter.sh/nodepool"], st.Condition.StringEquals["aws:RequestTag/karpenter.sh/nodepool"]) == ["api", "worker"] &&
        try(st.Condition.StringEquals["aws:ResourceTag/kubernetes.io/cluster/yeodam-v3-foundation"], st.Condition.StringEquals["aws:RequestTag/kubernetes.io/cluster/yeodam-v3-foundation"]) == "owned"
      ]) == 1
    )])
    error_message = "Mutations must stay on owned api/worker nodes/templates; never existing fixed nodes."
  }
  assert {
    condition = (
      length([for st in jsondecode(aws_iam_policy.discovery.policy).Statement : st if st.Sid == "PassDedicatedNodeRole" && st.Resource == aws_iam_role.node.arn && st.Condition.StringEquals["iam:PassedToService"] == "ec2.amazonaws.com"]) == 1 &&
      length([for st in jsondecode(aws_iam_policy.discovery.policy).Statement : st if st.Sid == "ProfileRead" && st.Resource == aws_iam_instance_profile.node.arn]) == 1 &&
      length([for st in jsondecode(aws_iam_policy.discovery.policy).Statement : st if st.Sid == "ClusterRead" && st.Resource == local.cluster_arn]) == 1 &&
      length([for st in jsondecode(aws_iam_policy.discovery.policy).Statement : st if st.Sid == "RegionalEC2Read" && st.Condition.StringEquals["aws:RequestedRegion"] == "ap-northeast-2"]) == 1 &&
      alltrue([for st in jsondecode(aws_iam_policy.discovery.policy).Statement : !contains(["iam:CreateInstanceProfile", "iam:AddRoleToInstanceProfile", "iam:CreateRole", "iam:*"], try(tostring(st.Action), ""))])
    )
    error_message = "PassRole, profile and EKS reads must use dedicated resources; IAM mutation is not delegated."
  }
  assert {
    condition = (
      aws_sqs_queue.interruption.sqs_managed_sse_enabled &&
      aws_sqs_queue.interruption.message_retention_seconds == 300 &&
      length(aws_cloudwatch_event_rule.interruption) == 4 &&
      alltrue([for rule in aws_cloudwatch_event_rule.interruption : jsondecode(rule.event_pattern).account == ["483175530259"] && jsondecode(rule.event_pattern).region == ["ap-northeast-2"]]) &&
      alltrue([for target in aws_cloudwatch_event_target.interruption : target.arn == aws_sqs_queue.interruption.arn]) &&
      jsondecode(aws_sqs_queue_policy.interruption.policy).Statement[0].Principal.Service == "events.amazonaws.com" &&
      length(jsondecode(aws_sqs_queue_policy.interruption.policy).Statement[0].Condition.ArnEquals["aws:SourceArn"]) == 4 &&
      jsondecode(aws_sqs_queue_policy.interruption.policy).Statement[0].Condition.StringEquals["aws:SourceAccount"] == "483175530259" &&
      jsondecode(aws_sqs_queue_policy.interruption.policy).Statement[1].Effect == "Deny" &&
      jsondecode(aws_sqs_queue_policy.interruption.policy).Statement[1].Condition.Bool["aws:SecureTransport"] == "false"
    )
    error_message = "Only account/region-selected EventBridge rules may send encrypted interruption messages over HTTPS."
  }
}
