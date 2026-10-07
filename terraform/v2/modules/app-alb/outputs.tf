output "alb_arn" {
  description = "Application Load Balancer ARN"
  value       = aws_lb.app.arn
}

output "alb_dns_name" {
  description = "AWS ALB DNS name; staging public Alias is added after healthy targets exist"
  value       = aws_lb.app.dns_name
}

output "alb_zone_id" {
  description = "ALB hosted zone ID for a later Route 53 Alias"
  value       = aws_lb.app.zone_id
}

output "alb_security_group_id" {
  description = "ALB source security group for FE and BE Task ingress"
  value       = aws_security_group.alb.id
}

output "frontend_target_group_arn" {
  description = "Frontend ECS Service target group"
  value       = aws_lb_target_group.frontend.arn
}

output "backend_target_group_arn" {
  description = "Backend ECS Service target group"
  value       = aws_lb_target_group.backend.arn
}
