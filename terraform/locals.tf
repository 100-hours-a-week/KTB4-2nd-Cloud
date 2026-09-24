locals {
  name_prefix                   = "${var.project_name}-${var.deployment_version}"
  parameter_store_path_prefix   = "/${var.project_name}/${var.deployment_version}"
  github_actions_deploy_subject = "repo:100-hours-a-week/KTB4-2nd-Cloud:environment:production"

  common_tags = {
    Project   = var.project_name
    Version   = var.deployment_version
    ManagedBy = "Terraform"
  }
}
