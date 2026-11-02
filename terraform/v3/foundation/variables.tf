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
  description = "One system node and one NAT per AZ; this initial foundation uses two AZs."
  type        = list(string)
  default     = ["ap-northeast-2a", "ap-northeast-2c"]

  validation {
    condition     = length(var.availability_zones) == 2 && length(distinct(var.availability_zones)) == 2 && alltrue([for az in var.availability_zones : can(regex("^ap-northeast-2[a-d]$", az))])
    error_message = "Choose two distinct Seoul AZs. A third Kafka AZ is a later network extension."
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

variable "node_instance_type" {
  description = "Initial x86 system capacity, not a measured application capacity."
  type        = string
  default     = "m6i.large"
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
