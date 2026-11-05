variable "aws_region" {
  type    = string
  default = "ap-northeast-2"

  validation {
    condition     = var.aws_region == "ap-northeast-2"
    error_message = "The foundation is scoped to the reviewed Seoul region."
  }
}

variable "expected_account_id" {
  description = "Reviewed AWS account; the provider rejects any other account."
  type        = string

  validation {
    condition     = can(regex("^[0-9]{12}$", var.expected_account_id))
    error_message = "Use a 12-digit account ID."
  }
}

variable "cluster_name" {
  type    = string
  default = "yeodam-v3-foundation"

  validation {
    condition     = can(regex("^yeodam-v3-[a-z0-9-]{1,40}$", var.cluster_name))
    error_message = "Use a V3-specific name, never an existing V2 service name."
  }
}

variable "kubernetes_version" {
  description = "Pin a standard-support release and recheck regional add-on compatibility before changing."
  type        = string
  default     = "1.36"
}

variable "vpc_cidr" {
  description = "Dedicated V3 /16; check ALL VPC/peering routes before applying."
  type        = string
  default     = "10.40.0.0/16"

  validation {
    condition     = can(cidrnetmask(var.vpc_cidr)) && can(regex("^10\\.[0-9]+\\.0\\.0/16$", var.vpc_cidr)) && !contains(["10.20.0.0/16", "10.30.0.0/16"], var.vpc_cidr)
    error_message = "Use a dedicated 10.x.0.0/16, excluding known V1/V2 ranges."
  }
}

variable "availability_zones" {
  description = "Network AZs in stable CIDR order. System nodes are configured independently. NAT sharing requires an explicit override."
  type        = list(string)
  default     = ["ap-northeast-2a", "ap-northeast-2c", "ap-northeast-2b"]

  validation {
    condition     = length(var.availability_zones) >= 2 && length(var.availability_zones) <= 3 && length(distinct(var.availability_zones)) == length(var.availability_zones) && try(var.availability_zones[0] == "ap-northeast-2a" && var.availability_zones[1] == "ap-northeast-2c", false) && alltrue([for az in var.availability_zones : can(regex("^ap-northeast-2[a-d]$", az))])
    error_message = "Keep existing a/c AZs in their original order; optionally append one distinct Seoul AZ to preserve subnet CIDRs."
  }
}

variable "admin_role_arns" {
  description = "Explicit operator IAM roles (not STS session ARNs); no implicit creator admin."
  type        = set(string)

  validation {
    condition     = length(var.admin_role_arns) > 0 && alltrue([for arn in var.admin_role_arns : can(regex("^arn:aws:iam::[0-9]{12}:role/.+$", arn))])
    error_message = "Supply at least one reviewed IAM role ARN."
  }
}

variable "api_public_access_cidrs" {
  description = "Operator public IPv4 /32 addresses; private API access remains enabled for nodes."
  type        = set(string)

  validation {
    condition     = length(var.api_public_access_cidrs) > 0 && alltrue([for cidr in var.api_public_access_cidrs : can(cidrnetmask(cidr)) && can(regex("/32$", cidr)) && cidr != "0.0.0.0/32"])
    error_message = "Use explicit operator IPv4 /32 addresses; public-wide API access is forbidden."
  }
}

variable "system_node_groups" {
  description = "Final two t3a.medium groups. Use reviewed migration inputs to add/verify/remove one AZ at a time; legacy m6i groups stay pinned to LT v1."
  type = map(object({
    availability_zone       = string
    instance_type           = string
    launch_template_version = optional(string)
  }))
  default = {
    ap-northeast-2a-t3a-medium = { availability_zone = "ap-northeast-2a", instance_type = "t3a.medium" }
    ap-northeast-2c-t3a-medium = { availability_zone = "ap-northeast-2c", instance_type = "t3a.medium" }
  }

  validation {
    condition = (
      length(var.system_node_groups) >= 2 && length(var.system_node_groups) <= 3 &&
      alltrue([for group in var.system_node_groups : contains(var.availability_zones, group.availability_zone)]) &&
      alltrue([for az in ["ap-northeast-2a", "ap-northeast-2c"] : contains([for group in var.system_node_groups : group.availability_zone], az)]) &&
      alltrue([for key, group in var.system_node_groups :
        startswith(key, group.availability_zone) && can(regex("^[a-z0-9-]{1,50}$", key)) &&
        contains(["m6i.large", "t3a.medium"], group.instance_type) &&
        (group.instance_type != "m6i.large" || group.launch_template_version == "1")
      ])
    )
    error_message = "Keep system groups in both original a/c AZs and within the network AZs, with 2-3 single-node groups. Only t3a.medium or legacy m6i.large pinned to LT v1 are allowed."
  }
}

variable "node_release_version" {
  description = "Pin the regionally available EKS AL2023 x86_64 standard release."
  type        = string
}

variable "addon_versions" {
  description = "Explicit versions returned by the regional EKS compatibility API. No latest-at-apply upgrades."
  type        = map(string)

  validation {
    condition     = toset(keys(var.addon_versions)) == toset(["vpc-cni", "kube-proxy", "coredns", "eks-pod-identity-agent", "aws-ebs-csi-driver"]) && alltrue([for version in values(var.addon_versions) : can(regex("^v[0-9]+\\.[0-9]+\\.[0-9]+-eksbuild\\.[0-9]+$", version))])
    error_message = "Pin exactly the five foundation add-ons to explicit EKS build versions."
  }
}

variable "nat_gateway_az_overrides" {
  description = "Explicit cost tradeoff: only the appended third AZ may share an existing a/c NAT. Existing AZ routes cannot change."
  type        = map(string)
  default     = {}
  validation {
    condition = alltrue([for az, target in var.nat_gateway_az_overrides :
      contains(slice(var.availability_zones, 2, length(var.availability_zones)), az) &&
      contains(slice(var.availability_zones, 0, 2), target)
    ])
    error_message = "Only the appended third AZ may use an existing a/c NAT; self references, cycles and changes to existing AZ routes are forbidden."
  }
}

variable "enable_kafka_nodes" {
  description = "Opt in only after reviewing the new three-node cost and Plan. Does not install Kafka."
  type        = bool
  default     = false
  validation {
    condition     = !var.enable_kafka_nodes || length(var.availability_zones) == 3
    error_message = "Kafka nodes require three reviewed network AZs."
  }
}

variable "enable_data_nodes" {
  description = "Opt in to two reviewed storage nodes only; does not install databases or migrate data."
  type        = bool
  default     = false
  validation {
    condition     = !var.enable_data_nodes || var.enable_kafka_nodes
    error_message = "Keep the existing Kafka groups enabled when adding storage capacity."
  }
}
