locals {
  name_prefix                 = "${var.project_name}-${var.deployment_version}"
  parameter_store_path_prefix = "/${var.project_name}/${var.deployment_version}"

  common_tags = {
    Project   = var.project_name
    Version   = var.deployment_version
    ManagedBy = "Terraform"
  }
}
