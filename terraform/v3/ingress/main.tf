# Independent state. No production resources, app secrets, Node/ALB creation or state reads.
provider "aws" {
  region              = "ap-northeast-2"
  allowed_account_ids = ["483175530259"]
  default_tags { tags = { Project = "yeodam", Version = "v3", Environment = "foundation", ManagedBy = "Terraform", Issue = "552" } }
}
variable "enabled" {
  type    = bool
  default = false
}
variable "publish_dns" {
  type    = bool
  default = false
  validation {
    condition     = !var.publish_dns || (var.enabled && var.enable_waf)
    error_message = "Attach the V3 WAF before publishing validation DNS."
  }
}
variable "enable_waf" {
  type    = bool
  default = false
}

locals {
  cluster = "yeodam-v3-foundation"
  vpc     = "vpc-0ecc3132f3b2170e9"
  node_sg = "sg-068c6d24435af33fb"
  host    = "v3.yeodam-2gether.com"
  ports   = var.enabled ? toset(["3000", "8080", "8081", "9091"]) : toset([])
}
data "aws_security_group" "nodes" {
  count = var.enabled ? 1 : 0
  id    = local.node_sg
  lifecycle {
    postcondition {
      condition     = self.vpc_id == local.vpc && startswith(self.name, "eks-cluster-sg-yeodam-v3-foundation-")
      error_message = "Existing V3 cluster SG only."
    }
  }
}
data "aws_route53_zone" "public" {
  count        = var.enabled ? 1 : 0
  name         = "yeodam-2gether.com."
  private_zone = false
}
resource "aws_iam_role" "controller" {
  count = var.enabled ? 1 : 0
  name  = "${local.cluster}-load-balancer-controller"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Allow", Principal = { Service = "pods.eks.amazonaws.com" }, Action = ["sts:AssumeRole", "sts:TagSession"], Condition = {
    StringEquals = { "aws:SourceAccount" = "483175530259", "aws:RequestTag/kubernetes-namespace" = "kube-system", "aws:RequestTag/kubernetes-service-account" = "yeodam-v3-load-balancer-controller" },
    ArnEquals    = { "aws:SourceArn" = "arn:aws:eks:ap-northeast-2:483175530259:cluster/${local.cluster}" }
  } }] })
}
resource "aws_iam_role_policy" "controller" {
  count  = var.enabled ? 1 : 0
  name   = "v3-owned-alb-only"
  role   = aws_iam_role.controller[0].id
  policy = file("${path.module}/policies/controller.json")
}
resource "aws_eks_pod_identity_association" "controller" {
  count           = var.enabled ? 1 : 0
  cluster_name    = local.cluster
  namespace       = "kube-system"
  service_account = "yeodam-v3-load-balancer-controller"
  role_arn        = aws_iam_role.controller[0].arn
}
resource "aws_security_group" "alb" {
  count       = var.enabled ? 1 : 0
  name        = "${local.cluster}-public-alb"
  description = "V3 validation ALB only; Controller cannot mutate SG rules"
  vpc_id      = local.vpc
}
resource "aws_vpc_security_group_ingress_rule" "public" {
  for_each          = var.enabled ? toset(["80", "443"]) : toset([])
  security_group_id = aws_security_group.alb[0].id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = tonumber(each.key)
  to_port           = tonumber(each.key)
}
resource "aws_vpc_security_group_egress_rule" "targets" {
  for_each                     = local.ports
  security_group_id            = aws_security_group.alb[0].id
  referenced_security_group_id = data.aws_security_group.nodes[0].id
  ip_protocol                  = "tcp"
  from_port                    = tonumber(each.key)
  to_port                      = tonumber(each.key)
}
resource "aws_vpc_security_group_ingress_rule" "targets" {
  for_each                     = local.ports
  security_group_id            = data.aws_security_group.nodes[0].id
  referenced_security_group_id = aws_security_group.alb[0].id
  ip_protocol                  = "tcp"
  from_port                    = tonumber(each.key)
  to_port                      = tonumber(each.key)
}
resource "aws_acm_certificate" "validation" {
  count             = var.enabled ? 1 : 0
  domain_name       = local.host
  validation_method = "DNS"
  lifecycle { create_before_destroy = true }
}
resource "aws_route53_record" "certificate" {
  for_each        = var.enabled ? toset([local.host]) : toset([])
  zone_id         = data.aws_route53_zone.public[0].zone_id
  name            = one(aws_acm_certificate.validation[0].domain_validation_options).resource_record_name
  type            = one(aws_acm_certificate.validation[0].domain_validation_options).resource_record_type
  records         = [one(aws_acm_certificate.validation[0].domain_validation_options).resource_record_value]
  ttl             = 60
  allow_overwrite = false
}
resource "aws_acm_certificate_validation" "validation" {
  count                   = var.enabled ? 1 : 0
  certificate_arn         = aws_acm_certificate.validation[0].arn
  validation_record_fqdns = [for r in aws_route53_record.certificate : r.fqdn]
}
data "aws_lb" "validation" {
  count = var.enabled && (var.publish_dns || var.enable_waf) ? 1 : 0
  name  = "yeodam-v3-validation"
  lifecycle {
    postcondition {
      condition     = self.vpc_id == local.vpc && self.tags["elbv2.k8s.aws/cluster"] == local.cluster
      error_message = "Only the controller's V3 ALB may receive the validation DNS name."
    }
  }
}
resource "aws_route53_record" "validation" {
  count           = var.enabled && var.publish_dns ? 1 : 0
  zone_id         = data.aws_route53_zone.public[0].zone_id
  name            = local.host
  type            = "A"
  allow_overwrite = false
  alias {
    name                   = data.aws_lb.validation[0].dns_name
    zone_id                = data.aws_lb.validation[0].zone_id
    evaluate_target_health = true
  }
}
output "alb_security_group_id" { value = var.enabled ? aws_security_group.alb[0].id : null }
output "certificate_arn" { value = var.enabled ? aws_acm_certificate_validation.validation[0].certificate_arn : null }
output "host" { value = local.host }

# Managed here, not by Controller IAM. Rates are observation-only until measured.
resource "aws_wafv2_web_acl" "validation" {
  count = var.enabled && var.enable_waf ? 1 : 0
  name  = "yeodam-v3-validation"
  scope = "REGIONAL"
  default_action {
    allow {}
  }
  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = "yeodam-v3-validation"
    sampled_requests_enabled   = false
  }
  rule {
    name     = "block-internal"
    priority = 0
    action {
      block {}
    }
    statement {
      or_statement {
        statement {
          byte_match_statement {
            positional_constraint = "STARTS_WITH"
            search_string         = "/api/internal"
            field_to_match {
              uri_path {}
            }
            text_transformation {
              priority = 0
              type     = "NONE"
            }
          }
        }
        statement {
          byte_match_statement {
            positional_constraint = "STARTS_WITH"
            search_string         = "/api/actuator"
            field_to_match {
              uri_path {}
            }
            text_transformation {
              priority = 0
              type     = "NONE"
            }
          }
        }
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "v3-block-internal"
      sampled_requests_enabled   = false
    }
  }
  dynamic "rule" {
    for_each = { auth = { priority = 1, limit = 500, path = "/api/auth/" }, api = { priority = 2, limit = 5000, path = "/api/" } }
    content {
      name     = "observe-${rule.key}"
      priority = rule.value.priority
      action {
        count {}
      }
      statement {
        rate_based_statement {
          aggregate_key_type    = "IP"
          evaluation_window_sec = 300
          limit                 = rule.value.limit
          scope_down_statement {
            byte_match_statement {
              positional_constraint = "STARTS_WITH"
              search_string         = rule.value.path
              field_to_match {
                uri_path {}
              }
              text_transformation {
                priority = 0
                type     = "NONE"
              }
            }
          }
        }
      }
      visibility_config {
        cloudwatch_metrics_enabled = true
        metric_name                = "v3-observe-${rule.key}"
        sampled_requests_enabled   = false
      }
    }
  }
}
resource "aws_wafv2_web_acl_association" "validation" {
  count        = var.enabled && var.enable_waf ? 1 : 0
  resource_arn = data.aws_lb.validation[0].arn
  web_acl_arn  = aws_wafv2_web_acl.validation[0].arn
}
