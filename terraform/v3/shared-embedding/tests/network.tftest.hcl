mock_provider "aws" {}
run "disabled" {
  command = plan
  assert {
    condition     = length(aws_vpc_peering_connection.embedding) == 0 && length(aws_route.to_embedding) == 0 && length(aws_route.to_v3) == 0 && length(aws_vpc_security_group_ingress_rule.embedding) == 0 && length(aws_route53_zone_association.embedding) == 0
    error_message = "Default must create no resources."
  }
}
run "minimal_private_path" {
  command = plan
  variables { enabled = true }
  assert {
    condition     = length(aws_vpc_peering_connection.embedding) == 1 && length(aws_route.to_embedding) == 3 && length(aws_route.to_v3) == 1 && length(aws_vpc_security_group_ingress_rule.embedding) == 1 && length(aws_route53_zone_association.embedding) == 1
    error_message = "Exactly seven path-only resources."
  }
  assert {
    condition     = alltrue([for r in aws_route.to_embedding : r.destination_cidr_block == "10.30.10.162/32"]) && aws_route.to_v3[0].destination_cidr_block == "10.40.0.0/16"
    error_message = "No general GPU VPC or default route."
  }
  assert {
    condition     = aws_vpc_security_group_ingress_rule.embedding[0].from_port == 8000 && aws_vpc_security_group_ingress_rule.embedding[0].to_port == 8000 && aws_vpc_security_group_ingress_rule.embedding[0].referenced_security_group_id == "sg-068c6d24435af33fb"
    error_message = "Only embedding TCP from V3 node SG."
  }
}
