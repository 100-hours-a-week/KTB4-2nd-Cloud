# Historical configuration is retained for review, not automatic recreation.
variable "allow_retired_environment_apply" {
  type    = bool
  default = false
}
resource "terraform_data" "retired_environment_guard" {
  input = var.allow_retired_environment_apply
  lifecycle {
    precondition {
      condition     = var.allow_retired_environment_apply
      error_message = "V2 production is retired. Use terraform/v2/retirement/production for the reviewed retained-only configuration."
    }
  }
}
