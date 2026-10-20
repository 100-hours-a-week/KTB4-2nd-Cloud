resource "aws_acm_certificate" "grafana" {
  domain_name       = var.domain_name
  validation_method = "DNS"
  tags              = var.tags
  lifecycle { create_before_destroy = true }
}

resource "aws_route53_record" "validation" {
  for_each = {
    for option in aws_acm_certificate.grafana.domain_validation_options :
    option.domain_name => option
  }
  zone_id = var.hosted_zone_id
  name    = each.value.resource_record_name
  type    = each.value.resource_record_type
  ttl     = 60
  records = [each.value.resource_record_value]
}

resource "aws_acm_certificate_validation" "grafana" {
  certificate_arn         = aws_acm_certificate.grafana.arn
  validation_record_fqdns = [for record in aws_route53_record.validation : record.fqdn]
}

resource "aws_lb_listener_certificate" "grafana" {
  listener_arn    = var.listener_arn
  certificate_arn = aws_acm_certificate_validation.grafana.certificate_arn
}

resource "aws_lb_target_group" "grafana" {
  name                 = "${var.name_prefix}-grafana"
  port                 = 3000
  protocol             = "HTTP"
  target_type          = "ip"
  vpc_id               = var.vpc_id
  deregistration_delay = 30
  health_check {
    path                = "/api/health"
    matcher             = "200"
    interval            = 30
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }
  tags = var.tags
}

resource "aws_lb_target_group_attachment" "grafana" {
  target_group_arn = aws_lb_target_group.grafana.arn
  target_id        = var.private_ip
  port             = 3000
}

# Grafana's internal /metrics can be unauthenticated. Never publish it.
# Both rules precede the existing application /api/* rule (priority 20).
resource "aws_lb_listener_rule" "deny_metrics" {
  listener_arn = var.listener_arn
  priority     = 1
  action {
    type = "fixed-response"
    fixed_response {
      content_type = "text/plain"
      status_code  = "404"
      message_body = "Not found"
    }
  }
  condition {
    host_header { values = [var.domain_name] }
  }
  condition {
    path_pattern { values = ["/metrics", "/metrics/*"] }
  }
}

resource "aws_lb_listener_rule" "grafana" {
  listener_arn = var.listener_arn
  priority     = 2
  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.grafana.arn
  }
  condition {
    host_header { values = [var.domain_name] }
  }
  depends_on = [aws_lb_listener_certificate.grafana, aws_lb_listener_rule.deny_metrics]
}

resource "aws_vpc_security_group_ingress_rule" "grafana" {
  security_group_id            = var.plg_security_group_id
  referenced_security_group_id = var.alb_security_group_id
  description                  = "Grafana from the environment ALB only"
  ip_protocol                  = "tcp"
  from_port                    = 3000
  to_port                      = 3000
}

resource "aws_route53_record" "grafana" {
  zone_id = var.hosted_zone_id
  name    = var.domain_name
  type    = "A"
  alias {
    name                   = var.alb_dns_name
    zone_id                = var.alb_zone_id
    evaluate_target_health = true
  }
  depends_on = [aws_lb_listener_rule.grafana]
}

output "url" { value = "https://${var.domain_name}" }
output "target_group_arn" { value = aws_lb_target_group.grafana.arn }
