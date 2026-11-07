mock_provider "aws" {
  mock_data "aws_lb" { defaults = { vpc_id = "vpc-0ecc3132f3b2170e9", tags = { "elbv2.k8s.aws/cluster" = "yeodam-v3-foundation" }, arn = "arn:aws:elasticloadbalancing:ap-northeast-2:483175530259:loadbalancer/app/yeodam-v3-validation/1234567890abcdef", dns_name = "fixture.ap-northeast-2.elb.amazonaws.com", zone_id = "Z0123456789ABCDEFG" } }
  mock_data "aws_security_group" { defaults = { id = "sg-068c6d24435af33fb", vpc_id = "vpc-0ecc3132f3b2170e9", name = "eks-cluster-sg-yeodam-v3-foundation-185284093" } }
  mock_data "aws_route53_zone" { defaults = { zone_id = "Z0123456789ABCDEFG" } }
  mock_resource "aws_iam_role" { defaults = { arn = "arn:aws:iam::483175530259:role/yeodam-v3-foundation-load-balancer-controller" } }
  mock_resource "aws_acm_certificate" { defaults = {
    arn                       = "arn:aws:acm:ap-northeast-2:483175530259:certificate/00000000-0000-0000-0000-000000000001"
    domain_validation_options = [{ domain_name = "v3.yeodam-2gether.com", resource_record_name = "_fixture.v3.yeodam-2gether.com", resource_record_type = "CNAME", resource_record_value = "_fixture.acm-validations.aws." }]
  } }
}
run "disabled" {
  command = plan
  assert {
    condition     = length(aws_iam_role.controller) == 0 && length(aws_security_group.alb) == 0 && length(aws_route53_record.validation) == 0
    error_message = "Disabled must provision nothing."
  }
}
run "minimum_ingress" {
  command = plan
  variables { enabled = true }
  assert {
    condition     = length(aws_iam_role.controller) == 1 && length(aws_eks_pod_identity_association.controller) == 1 && length(aws_security_group.alb) == 1 && length(aws_vpc_security_group_ingress_rule.targets) == 4 && length(aws_vpc_security_group_egress_rule.targets) == 4 && length(aws_route53_record.validation) == 0
    error_message = "Only minimum identity, one ALB SG and exact ports; no DNS publish by default."
  }
  assert {
    condition     = aws_acm_certificate.validation[0].domain_name == "v3.yeodam-2gether.com" && aws_route53_record.certificate["v3.yeodam-2gether.com"].allow_overwrite == false
    error_message = "Separate validation certificate only; never overwrite an existing record."
  }
  assert {
    condition     = jsondecode(aws_iam_role.controller[0].assume_role_policy).Statement[0].Condition.StringEquals["aws:RequestTag/kubernetes-service-account"] == "yeodam-v3-load-balancer-controller"
    error_message = "Exact Controller Pod Identity required."
  }
}

run "publish_validation_only" {
  command = plan
  variables {
    enabled     = true
    publish_dns = true
    enable_waf  = true
  }
  assert {
    condition     = aws_route53_record.validation[0].name == "v3.yeodam-2gether.com" && aws_route53_record.validation[0].allow_overwrite == false
    error_message = "Only a new V3 validation alias may be published."
  }
}
run "reject_wrong_vpc" {
  command = plan
  variables { enabled = true }
  override_data {
    target = data.aws_security_group.nodes[0]
    values = { id = "sg-068c6d24435af33fb", vpc_id = "vpc-00000000000000000", name = "eks-cluster-sg-yeodam-v3-foundation-185284093" }
  }
  expect_failures = [data.aws_security_group.nodes]
}

run "waf_before_dns" {
  command = plan
  variables {
    enabled    = true
    enable_waf = true
  }
  assert {
    condition     = length(aws_wafv2_web_acl.validation) == 1 && length(aws_wafv2_web_acl_association.validation) == 1 && length(aws_route53_record.validation) == 0 && aws_wafv2_web_acl.validation[0].scope == "REGIONAL"
    error_message = "Attach exactly one private-sampling-disabled V3 WAF before DNS."
  }
  assert {
    condition     = length([for r in aws_wafv2_web_acl.validation[0].rule : r if length(r.action[0].count) > 0]) == 2 && !aws_wafv2_web_acl.validation[0].visibility_config[0].sampled_requests_enabled
    error_message = "Rate rules must remain Count during initial measurement, with request samples disabled."
  }
}
