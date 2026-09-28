resource "aws_cloudwatch_log_group" "nginx_access" {
  name              = "/${var.project_name}/${var.deployment_version}/nginx/access"
  retention_in_days = 30

  tags = {
    Name = "${local.name_prefix}-nginx-access"
    Role = "observability"
  }
}

resource "aws_cloudwatch_log_group" "nginx_error" {
  name              = "/${var.project_name}/${var.deployment_version}/nginx/error"
  retention_in_days = 30

  tags = {
    Name = "${local.name_prefix}-nginx-error"
    Role = "observability"
  }
}
