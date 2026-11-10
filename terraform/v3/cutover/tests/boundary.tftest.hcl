mock_provider "aws" {
  mock_data "aws_lb" {
    defaults = {
      vpc_id   = "vpc-0ecc3132f3b2170e9"
      dns_name = "yeodam-v3-validation-416234802.ap-northeast-2.elb.amazonaws.com"
      zone_id  = "ZWKZPGTI48KDX"
      tags     = { "elbv2.k8s.aws/cluster" = "yeodam-v3-foundation" }
    }
  }
}
override_resource {
  target = aws_route53_record.operating["root"]
  values = {
    zone_id = "Z09268031LIP9TICUQ911"
    name    = "yeodam-2gether.com"
    type    = "A"
    alias   = [{ name = "yeodam-v3-validation-416234802.ap-northeast-2.elb.amazonaws.com", zone_id = "ZWKZPGTI48KDX", evaluate_target_health = true }]
  }
}
run "disabled" {
  command = plan
  assert {
    condition     = length(aws_route53_record.operating) == 0
    error_message = "Merge must not change DNS."
  }
}
run "unreviewed" {
  command = plan
  variables { activate_dns = true }
  expect_failures = [aws_route53_record.operating["root"]]
}
run "fixed_destination" {
  command = plan
  variables {
    activate_dns                 = true
    approved_verification_sha256 = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  }
  assert {
    condition     = aws_route53_record.operating["root"].name == "yeodam-2gether.com" && aws_route53_record.operating["root"].type == "A"
    error_message = "Only the operating root A alias may be adopted."
  }
}
