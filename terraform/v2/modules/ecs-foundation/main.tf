resource "aws_ecs_cluster" "app" {
  name = "${var.name_prefix}-app"

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-app"
  })
}

resource "aws_cloudwatch_log_group" "frontend" {
  name              = "/yeodam/v2/${var.tags.Environment}/frontend"
  retention_in_days = var.log_retention_days

  tags = var.tags
}

resource "aws_cloudwatch_log_group" "backend" {
  name              = "/yeodam/v2/${var.tags.Environment}/backend"
  retention_in_days = var.log_retention_days

  tags = var.tags
}

resource "aws_secretsmanager_secret" "ghcr_credentials" {
  name        = var.secret_name
  description = "GHCR repository credentials for V2 ${var.tags.Environment} ECS task image pulls"

  tags = var.tags
}

data "aws_iam_policy_document" "ecs_task_assume_role" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "task_execution" {
  name               = "${var.name_prefix}-task-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_task_assume_role.json

  tags = var.tags
}

data "aws_iam_policy_document" "task_execution" {
  statement {
    sid       = "WriteFrontendAndBackendLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.frontend.arn}:*", "${aws_cloudwatch_log_group.backend.arn}:*"]
  }

  statement {
    sid       = "ReadGhcrCredentials"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.ghcr_credentials.arn]
  }
}

resource "aws_iam_role_policy" "task_execution" {
  name   = "${var.name_prefix}-task-execution"
  role   = aws_iam_role.task_execution.id
  policy = data.aws_iam_policy_document.task_execution.json
}
