# Import only the obsolete V1/V2 VPC association before retiring it. The zone,
# GPU association and independently owned V3 association remain protected.
variable "import_legacy_zone_association" {
  type    = bool
  default = false
}
resource "aws_route53_zone_association" "legacy_production" {
  for_each   = var.import_legacy_zone_association ? toset(["production"]) : toset([])
  zone_id    = aws_route53_zone.production_private.zone_id
  vpc_id     = "vpc-08b0e3afa68321309"
  vpc_region = "ap-northeast-2"
}
import {
  for_each = var.import_legacy_zone_association ? toset(["production"]) : toset([])
  to       = aws_route53_zone_association.legacy_production[each.key]
  id       = "Z0848608U1XFD63QO507:vpc-08b0e3afa68321309"
}
