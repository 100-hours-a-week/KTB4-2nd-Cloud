provider "aws" {
  region              = "ap-northeast-2"
  allowed_account_ids = ["483175530259"]
}
variable "activate_dns" {
  type    = bool
  default = false
}
variable "approved_verification_sha256" {
  type    = string
  default = ""
  validation {
    condition     = var.approved_verification_sha256 == "" || can(regex("^[a-f0-9]{64}$", var.approved_verification_sha256))
    error_message = "Use the exact verified cutover receipt hash."
  }
}
data "aws_lb" "v3" {
  count = var.activate_dns ? 1 : 0
  name  = "yeodam-v3-validation"
  lifecycle {
    postcondition {
      condition     = self.vpc_id == "vpc-0ecc3132f3b2170e9" && self.tags["elbv2.k8s.aws/cluster"] == "yeodam-v3-foundation"
      error_message = "Use only the existing V3 ALB."
    }
  }
}
# The existing root A record was not managed by the V2 ALB module.
# Adopt it only during explicitly approved activation; do not create a second owner.
import {
  for_each = var.activate_dns ? toset(["root"]) : toset([])
  to       = aws_route53_record.operating[each.key]
  id       = "Z09268031LIP9TICUQ911_yeodam-2gether.com_A"
}
resource "aws_route53_record" "operating" {
  for_each        = var.activate_dns ? toset(["root"]) : toset([])
  zone_id         = "Z09268031LIP9TICUQ911"
  name            = "yeodam-2gether.com"
  type            = "A"
  allow_overwrite = false
  alias {
    name                   = data.aws_lb.v3[0].dns_name
    zone_id                = data.aws_lb.v3[0].zone_id
    evaluate_target_health = true
  }
  lifecycle {
    prevent_destroy = true
    precondition {
      condition     = length(var.approved_verification_sha256) == 64
      error_message = "Verify data, approved CD and all ALB targets before changing public DNS."
    }
  }
}
# The existing operating ACM certificate is shared after cutover. Preserve it
# during V2 retirement; do not delete its V2 state resource or validation CNAME.
