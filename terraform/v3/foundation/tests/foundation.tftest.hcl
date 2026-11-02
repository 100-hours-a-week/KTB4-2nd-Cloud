mock_provider "aws" {
  mock_resource "aws_launch_template" {
    defaults = { id = "lt-0123456789abcdef0", latest_version = 1 }
  }
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::483175530259:role/mock-foundation" }
  }
  mock_resource "aws_eks_cluster" {
    defaults = {
      identity = [{ oidc = [{ issuer = "https://oidc.eks.ap-northeast-2.amazonaws.com/id/TEST" }] }]
    }
  }
  mock_resource "aws_iam_openid_connect_provider" {
    defaults = { arn = "arn:aws:iam::483175530259:oidc-provider/example.test" }
  }
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" }
  }
}

variables {
  expected_account_id     = "483175530259"
  admin_role_arns         = ["arn:aws:iam::483175530259:role/test-operator"]
  api_public_access_cidrs = ["192.0.2.10/32"]
  node_release_version    = "1.36.5-20261031"
  addon_versions = {
    vpc-cni                = "v1.23.2-eksbuild.8"
    kube-proxy             = "v1.36.0-eksbuild.60"
    coredns                = "v1.14.7-eksbuild.28"
    eks-pod-identity-agent = "v1.4.1-eksbuild.3"
    aws-ebs-csi-driver     = "v1.67.2-eksbuild.1"
  }
}

run "isolated_two_az_foundation" {
  # All providers are mocked: resolve computed IDs without calling AWS.
  command = apply

  assert {
    condition     = length(aws_eks_node_group.system) == 2 && alltrue([for group in aws_eks_node_group.system : length(group.subnet_ids) == 1 && group.scaling_config[0].desired_size == 1])
    error_message = "Keep exactly one initial system node per AZ, not an unconstrained shared ASG."
  }
  assert {
    condition     = alltrue([for subnet in aws_subnet.private : subnet.map_public_ip_on_launch == false]) && alltrue([for az, route in aws_route.private : route.nat_gateway_id == aws_nat_gateway.this[az].id])
    error_message = "Private nodes must use their own AZ NAT."
  }
  assert {
    condition     = aws_eks_cluster.this.access_config[0].bootstrap_cluster_creator_admin_permissions == false && aws_eks_cluster.this.access_config[0].authentication_mode == "API" && aws_eks_cluster.this.vpc_config[0].endpoint_private_access
    error_message = "Access must be explicit, with the private API enabled for nodes."
  }
  assert {
    condition     = !contains(keys(aws_iam_role_policy_attachment.node), "AmazonEKS_CNI_Policy") && !contains(keys(aws_iam_role_policy_attachment.node), "AmazonEBSCSIDriverPolicyV2")
    error_message = "Do not grant CNI or storage modification permissions to every node."
  }
  assert {
    condition     = aws_launch_template.system.metadata_options[0].http_tokens == "required" && aws_launch_template.system.metadata_options[0].http_put_response_hop_limit == 1 && aws_launch_template.system.block_device_mappings[0].ebs[0].encrypted
    error_message = "Keep IMDSv2, block pod metadata inheritance and encrypt node disks."
  }
  assert {
    condition     = alltrue([for group in aws_eks_node_group.system : group.instance_types == tolist(["t3a.medium"])]) && aws_launch_template.system.credit_specification[0].cpu_credits == "unlimited"
    error_message = "Final defaults must use the reviewed x86 size and an explicit credit mode."
  }
}

run "keep_legacy_template_during_add_a" {
  command = plan
  variables {
    system_node_groups = {
      ap-northeast-2a            = { availability_zone = "ap-northeast-2a", instance_type = "m6i.large", launch_template_version = "1" }
      ap-northeast-2c            = { availability_zone = "ap-northeast-2c", instance_type = "m6i.large", launch_template_version = "1" }
      ap-northeast-2a-t3a-medium = { availability_zone = "ap-northeast-2a", instance_type = "t3a.medium" }
    }
  }
  assert {
    condition = (
      length(aws_eks_node_group.system) == 3 &&
      aws_eks_node_group.system["ap-northeast-2a"].launch_template[0].version == "1" &&
      aws_eks_node_group.system["ap-northeast-2c"].launch_template[0].version == "1"
    )
    error_message = "Legacy nodes must not roll onto the new T3a credit template."
  }
}

run "reject_unpinned_legacy" {
  command = plan
  variables {
    system_node_groups = {
      ap-northeast-2a = { availability_zone = "ap-northeast-2a", instance_type = "m6i.large" }
      ap-northeast-2c = { availability_zone = "ap-northeast-2c", instance_type = "m6i.large" }
    }
  }
  expect_failures = [var.system_node_groups]
}

run "reject_public_api" {
  command = plan
  variables { api_public_access_cidrs = ["0.0.0.0/0"] }
  expect_failures = [var.api_public_access_cidrs]
}

run "reject_v2_network" {
  command = plan
  variables { vpc_cidr = "10.30.0.0/16" }
  expect_failures = [var.vpc_cidr]
}

run "reject_duplicate_az" {
  command = plan
  variables { availability_zones = ["ap-northeast-2a", "ap-northeast-2a"] }
  expect_failures = [var.availability_zones]
}

run "reject_external_admin" {
  command = plan
  variables { admin_role_arns = ["arn:aws:iam::111111111111:role/external"] }
  expect_failures = [check.admin_accounts, aws_eks_cluster.this]
}

run "reject_missing_addon" {
  command = plan
  variables { addon_versions = {} }
  expect_failures = [var.addon_versions]
}
