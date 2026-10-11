variable "aws_region" {
  description = "AWS Region for the V2 staging network"
  type        = string
  default     = "ap-northeast-2"

  validation {
    condition     = var.aws_region == "ap-northeast-2"
    error_message = "V2 staging must be deployed in ap-northeast-2."
  }
}

variable "vpc_cidr" {
  description = "Proposed, non-overlapping V2 staging VPC CIDR; verify against account VPCs before apply"
  type        = string
  default     = "10.30.0.0/16"

  validation {
    condition = (
      can(cidrsubnet(var.vpc_cidr, 8, 10)) &&
      try(tonumber(split("/", var.vpc_cidr)[1]), -1) == 16
    )
    error_message = "vpc_cidr must be a valid IPv4 /16 CIDR for the derived /24 subnets."
  }
}

variable "app_availability_zone" {
  description = "Availability Zone for App, MySQL, Redis and the NAT gateway"
  type        = string
  default     = "ap-northeast-2a"
}

variable "alb_secondary_availability_zone" {
  description = "Second Availability Zone reserved for the ALB public subnet"
  type        = string
  default     = "ap-northeast-2c"

  validation {
    condition     = var.alb_secondary_availability_zone != var.app_availability_zone
    error_message = "The two ALB public subnets must be in different availability zones."
  }
}

variable "mysql_ami_id" {
  description = "Reviewed Canonical Ubuntu 24.04 ARM64 AMI for the staging MySQL host"
  type        = string
  default     = "ami-0ccbfe1123f2d682d"
}

variable "mysql_active_private_ip" {
  description = "Validated MySQL private IP to publish in the private zone; null until recovery checks pass"
  type        = string
  default     = null

  validation {
    condition     = var.mysql_active_private_ip == null || can(cidrhost("${var.mysql_active_private_ip}/32", 0))
    error_message = "mysql_active_private_ip must be a valid IPv4 address or null."
  }
}

variable "redis_node_type" {
  description = "Initial staging Redis node size; revisit after auth-state load measurements"
  type        = string
  default     = "cache.t4g.micro"
}

variable "v1_source_ami_id" {
  description = "Reviewed Ubuntu 24.04 x86_64 AMI for the V1 rehearsal App and Worker"
  type        = string
  default     = "ami-086a43496cb46286c"
}

variable "v1_source_app_instance_type" {
  description = "V1 rehearsal App size; production parity is t3.medium"
  type        = string
  default     = "t3.medium"
}

variable "v1_source_worker_instance_type" {
  description = "V1 rehearsal AI Worker size; stop outside rehearsal windows"
  type        = string
  default     = "c7i.xlarge"
}

variable "v1_source_mysql_data_gib" {
  description = "Retained MySQL data volume for the V1 rehearsal source"
  type        = number
  default     = 20

  validation {
    condition     = var.v1_source_mysql_data_gib >= 10
    error_message = "The V1 rehearsal MySQL volume must be at least 10 GiB."
  }
}

variable "plg_instance_type" {
  description = "Private staging PLG host size; measure memory under rehearsal load"
  type        = string
  default     = "t3.medium"
}

variable "plg_data_gib" {
  description = "Retained EBS capacity for the staging PLG data stores"
  type        = number
  default     = 40

  validation {
    condition     = var.plg_data_gib >= 20
    error_message = "PLG data volume must have at least 20 GiB."
  }
}

variable "enable_load_generator" {
  description = "Create the temporary staging k6 host only during a rehearsal"
  type        = bool
  default     = false
}

variable "load_generator_instance_type" {
  description = "Staging k6 host size; verify generator headroom during each run"
  type        = string
  default     = "c6i.large"

  validation {
    condition     = contains(["c6i.large", "m6i.xlarge"], var.load_generator_instance_type)
    error_message = "The staging load generator must use c6i.large or m6i.xlarge."
  }
}

variable "frontend_image" {
  description = "Initial V2 staging frontend main image, pinned by digest; override for a later approved release"
  type        = string
  # KTB4-2nd-FE main bd50569, Frontend Image run 37902003903.
  default = "ghcr.io/100-hours-a-week/yeodam-frontend@sha256:2ff2e41c82c97ddcdf0e7ec6300cba42b756ef2a4cbd27971831fd772c05ab81"

  validation {
    condition     = var.frontend_image == null || can(regex("^ghcr[.]io/100-hours-a-week/yeodam-frontend@sha256:[0-9a-f]{64}$", var.frontend_image))
    error_message = "frontend_image must be a yeodam-frontend GHCR digest reference."
  }
}

variable "backend_image" {
  description = "Immutable GHCR backend image reference; null keeps the staging service absent until runtime dependencies are ready"
  type        = string
  default     = null

  validation {
    condition     = var.backend_image == null || can(regex("^ghcr[.]io/100-hours-a-week/yeodam-backend@sha256:[0-9a-f]{64}$", var.backend_image))
    error_message = "backend_image must be a yeodam-backend GHCR digest reference."
  }
}

variable "frontend_desired_count" {
  description = "Initial staging frontend task count; raise to two for distribution and rolling checks"
  type        = number
  default     = 1

  validation {
    condition     = var.frontend_desired_count >= 1 && var.frontend_desired_count <= 4 && floor(var.frontend_desired_count) == var.frontend_desired_count
    error_message = "frontend_desired_count must be an integer from 1 to 4."
  }
}

variable "backend_desired_count" {
  description = "Initial staging backend task count; raise to two after shared-state checks"
  type        = number
  default     = 1

  validation {
    condition     = var.backend_desired_count >= 1 && var.backend_desired_count <= 6 && floor(var.backend_desired_count) == var.backend_desired_count
    error_message = "backend_desired_count must be an integer from 1 to 6."
  }
}

variable "backend_environment" {
  description = "Non-secret Backend runtime values; provide required app contract values before enabling backend_image"
  type        = map(string)
  default     = {}

  validation {
    condition = length(setintersection(toset(keys(var.backend_environment)), toset([
      "MYSQL_PASSWORD", "KAKAO_CLIENT_SECRET", "JWT_SECRET", "AI_SERVER_API_KEY", "REDIS_PASSWORD",
    ]))) == 0
    error_message = "backend_environment must not contain credentials; use staging SSM parameters or Redis Secrets Manager."
  }
}
