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
  variables { availability_zones = ["ap-northeast-2a", "ap-northeast-2c"] }
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

run "three_az_network_keeps_two_system_nodes" {
  command = apply

  assert {
    condition = (
      length(aws_subnet.private) == 3 && length(aws_subnet.public) == 3 &&
      length(aws_nat_gateway.this) == 3 && length(aws_eip.nat) == 3 &&
      length(aws_route.private) == 3 && length(aws_vpc_endpoint.s3.route_table_ids) == 3 &&
      length(aws_eks_cluster.this.vpc_config[0].subnet_ids) == 3 &&
      length(aws_eks_node_group.system) == 2
    )
    error_message = "Extend network and EKS subnet coverage without adding system nodes."
  }
  assert {
    condition = (
      aws_subnet.public["ap-northeast-2a"].cidr_block == "10.40.0.0/24" &&
      aws_subnet.public["ap-northeast-2c"].cidr_block == "10.40.1.0/24" &&
      aws_subnet.private["ap-northeast-2a"].cidr_block == "10.40.16.0/20" &&
      aws_subnet.private["ap-northeast-2c"].cidr_block == "10.40.32.0/20" &&
      aws_subnet.public["ap-northeast-2b"].cidr_block == "10.40.2.0/24" &&
      aws_subnet.private["ap-northeast-2b"].cidr_block == "10.40.48.0/20"
    )
    error_message = "Preserve a/c CIDRs; allocate only the new b subnet ranges."
  }
  assert {
    condition     = alltrue([for az, route in aws_route.private : route.nat_gateway_id == aws_nat_gateway.this[az].id]) && alltrue([for group in aws_eks_node_group.system : group.scaling_config[0].desired_size == 1 && length(group.subnet_ids) == 1])
    error_message = "Keep per-AZ NAT routing and existing single-node groups."
  }
}

run "reject_reordered_existing_azs" {
  command = plan
  variables { availability_zones = ["ap-northeast-2a", "ap-northeast-2b", "ap-northeast-2c"] }
  expect_failures = [var.availability_zones]
}

run "reject_node_outside_network" {
  command = plan
  variables {
    availability_zones = ["ap-northeast-2a", "ap-northeast-2c"]
    system_node_groups = {
      ap-northeast-2a-t3a-medium = { availability_zone = "ap-northeast-2a", instance_type = "t3a.medium" }
      ap-northeast-2c-t3a-medium = { availability_zone = "ap-northeast-2c", instance_type = "t3a.medium" }
      ap-northeast-2b-t3a-medium = { availability_zone = "ap-northeast-2b", instance_type = "t3a.medium" }
    }
  }
  expect_failures = [var.system_node_groups]
}

run "reject_removing_original_system_az" {
  command = plan
  variables {
    system_node_groups = {
      ap-northeast-2a-t3a-medium = { availability_zone = "ap-northeast-2a", instance_type = "t3a.medium" }
      ap-northeast-2b-t3a-medium = { availability_zone = "ap-northeast-2b", instance_type = "t3a.medium" }
    }
  }
  expect_failures = [var.system_node_groups]
}

run "kafka_nodes_are_opt_in" {
  command = apply
  assert {
    condition     = length(aws_eks_node_group.kafka) == 0 && length(aws_launch_template.kafka) == 0
    error_message = "Default plans must not add billed Kafka capacity."
  }
}

run "small_kafka_nodes_with_shared_third_az_nat" {
  command = apply
  variables {
    enable_kafka_nodes       = true
    nat_gateway_az_overrides = { ap-northeast-2b = "ap-northeast-2a" }
  }
  assert {
    condition     = length(aws_eks_node_group.kafka) == 3 && length(aws_eks_node_group.system) == 2 && length(aws_nat_gateway.this) == 2
    error_message = "Keep three small Kafka nodes, two unchanged system nodes and no new third NAT."
  }
  assert {
    condition = alltrue([for az, group in aws_eks_node_group.kafka :
      group.instance_types == tolist(["t3a.small"]) &&
      toset(group.subnet_ids) == toset([aws_subnet.private[az].id]) &&
      group.scaling_config[0].desired_size == 1 && group.scaling_config[0].min_size == 1 && group.scaling_config[0].max_size == 1 &&
      group.capacity_type == "ON_DEMAND" && group.ami_type == "AL2023_x86_64_STANDARD" &&
      group.labels["yeodam.io/role"] == "kafka" &&
      one(group.taint).key == "yeodam.io/role" && one(group.taint).value == "kafka" && one(group.taint).effect == "NO_SCHEDULE"
    ])
    error_message = "Bound capacity and enforce one AZ per Kafka node with explicit placement identity/taint."
  }
  assert {
    condition = (
      aws_launch_template.kafka[0].credit_specification[0].cpu_credits == "standard" &&
      aws_launch_template.kafka[0].block_device_mappings[0].ebs[0].volume_size == 20 &&
      aws_launch_template.kafka[0].block_device_mappings[0].ebs[0].encrypted &&
      aws_launch_template.kafka[0].metadata_options[0].http_tokens == "required" &&
      aws_launch_template.kafka[0].metadata_options[0].http_put_response_hop_limit == 1
    )
    error_message = "Avoid surplus CPU charges, keep small encrypted root disks and deny Pod IMDS access."
  }
  assert {
    condition = (
      aws_route.private["ap-northeast-2b"].nat_gateway_id == aws_nat_gateway.this["ap-northeast-2a"].id &&
      aws_route.private["ap-northeast-2c"].nat_gateway_id == aws_nat_gateway.this["ap-northeast-2c"].id &&
      aws_route.private["ap-northeast-2a"].nat_gateway_id == aws_nat_gateway.this["ap-northeast-2a"].id
    )
    error_message = "Only the new third AZ may share NAT; existing AZ routes remain unchanged."
  }
}

run "reject_kafka_on_two_az_network" {
  command = plan
  variables {
    availability_zones = ["ap-northeast-2a", "ap-northeast-2c"]
    enable_kafka_nodes = true
  }
  expect_failures = [var.enable_kafka_nodes]
}

run "reject_changing_existing_nat_route" {
  command = plan
  variables { nat_gateway_az_overrides = { ap-northeast-2a = "ap-northeast-2c" } }
  expect_failures = [var.nat_gateway_az_overrides]
}

run "reject_third_az_self_nat_override" {
  command = plan
  variables { nat_gateway_az_overrides = { ap-northeast-2b = "ap-northeast-2b" } }
  expect_failures = [var.nat_gateway_az_overrides]
}
