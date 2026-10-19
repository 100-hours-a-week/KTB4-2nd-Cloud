# Separate runtime for the V1-to-V2 A rehearsal. The normal staging Backend
# continues to use the validation DB and its own Redis/JWT contract.
locals {
  a_rehearsal_enabled = var.a_rehearsal_run_id != null
  a_rehearsal_origin  = "https://v1-staging.yeodam-2gether.com"
  a_rehearsal_environment = {
    MYSQL_URL                           = "jdbc:mysql://${module.mysql.private_ip}:3306/yeodam_v1_source?serverTimezone=Asia/Seoul&characterEncoding=utf8"
    MYSQL_USERNAME                      = "yeodam_app"
    SPRING_FLYWAY_ENABLED               = "false"
    SPRING_JPA_HIBERNATE_DDL_AUTO       = "validate"
    AUTH_SESSION_KEY_PREFIX             = var.a_rehearsal_run_id == null ? "" : "yeodam:cutover:${var.a_rehearsal_run_id}:"
    JWT_ISSUER                          = local.a_rehearsal_origin
    FRONTEND_ORIGIN                     = local.a_rehearsal_origin
    FRONTEND_OAUTH_CALLBACK_URI         = "${local.a_rehearsal_origin}/auth/callback"
    KAKAO_REDIRECT_URI                  = "${local.a_rehearsal_origin}/api/auth/kakao/callback"
    APP_TRIP_RESULT_LISTENER_ENABLED    = "false"
    STORY_RESULT_LISTENER_ENABLED       = "false"
    APP_OUTBOX_RECEIPT_LISTENER_ENABLED = "false"
    STORY_STATUS_POLLING_ENABLED        = "false"
    SPRING_KAFKA_LISTENER_AUTO_STARTUP  = "false"
    SENTRY_ENVIRONMENT                  = "staging-a-rehearsal"
  }
}

resource "aws_iam_role_policy" "a_rehearsal_source_redis_read" {
  count = local.a_rehearsal_enabled ? 1 : 0
  name  = "yeodam-a-rehearsal-redis-read"
  role  = aws_iam_role.v1_source_app.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "secretsmanager:GetSecretValue"
      Resource = module.redis.auth_token_secret_arn
    }]
  })
}

resource "aws_iam_role" "a_rehearsal_execution" {
  count              = local.a_rehearsal_enabled ? 1 : 0
  name               = "yeodam-v2-staging-a-rehearsal-execution"
  assume_role_policy = data.aws_iam_policy_document.backend_task_assume_role.json
  tags               = local.common_tags
}

data "aws_iam_policy_document" "a_rehearsal_execution" {
  count = local.a_rehearsal_enabled ? 1 : 0

  statement {
    sid       = "ReadImageAndRedisCredentials"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [module.ecs_foundation.ghcr_credentials_secret_arn, module.redis.auth_token_secret_arn]
  }

  statement {
    sid     = "ReadRehearsalRuntimeParameters"
    actions = ["ssm:GetParameters"]
    resources = concat(
      [for name in local.backend_secret_names : "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter${local.backend_parameter_prefix}/${name}" if name != "JWT_SECRET"],
      [
        "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter${local.v1_source_parameter_prefix}/app/JWT_SECRET",
        "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter${local.mysql_app_password_parameter}",
      ]
    )
  }

  statement {
    sid       = "WriteBackendLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${module.ecs_foundation.backend_log_group_arn}:*"]
  }
}

resource "aws_iam_role_policy" "a_rehearsal_execution" {
  count  = local.a_rehearsal_enabled ? 1 : 0
  name   = "yeodam-v2-staging-a-rehearsal-execution"
  role   = aws_iam_role.a_rehearsal_execution[0].id
  policy = data.aws_iam_policy_document.a_rehearsal_execution[0].json
}

resource "aws_lb_target_group" "a_rehearsal_backend" {
  count                = local.a_rehearsal_enabled ? 1 : 0
  name                 = "yeodam-v2-stg-a-be"
  port                 = 8080
  protocol             = "HTTP"
  target_type          = "ip"
  vpc_id               = module.network.vpc_id
  deregistration_delay = 60

  health_check {
    path                = "/api/actuator/health"
    protocol            = "HTTP"
    matcher             = "200"
    interval            = 15
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 2
  }

  tags = merge(local.common_tags, { Name = "yeodam-v2-staging-a-rehearsal-be" })
}

resource "aws_lb_listener_rule" "a_rehearsal_backend" {
  count        = local.a_rehearsal_enabled ? 1 : 0
  listener_arn = module.app_alb.https_listener_arn
  priority     = 15

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.a_rehearsal_backend[0].arn
  }

  condition {
    host_header { values = ["v1-staging.yeodam-2gether.com"] }
  }
  condition {
    http_header {
      http_header_name = "X-Yeodam-A-Rehearsal"
      values           = [var.a_rehearsal_run_id]
    }
  }
  condition {
    path_pattern { values = ["/api/*"] }
  }

  tags = local.common_tags
}

resource "aws_ecs_task_definition" "a_rehearsal_backend" {
  count                    = local.a_rehearsal_enabled ? 1 : 0
  family                   = "yeodam-v2-staging-a-rehearsal-backend"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "1024"
  memory                   = "2048"
  execution_role_arn       = aws_iam_role.a_rehearsal_execution[0].arn
  task_role_arn            = aws_iam_role.backend_task.arn

  container_definitions = jsonencode([{
    name      = "backend"
    image     = var.a_rehearsal_backend_image
    essential = true
    repositoryCredentials = {
      credentialsParameter = module.ecs_foundation.ghcr_credentials_secret_arn
    }
    portMappings = [
      { containerPort = 8080, hostPort = 8080, protocol = "tcp" },
      { containerPort = 9091, hostPort = 9091, protocol = "tcp" },
    ]
    environment = [for name, value in merge(var.backend_environment, local.backend_base_environment, local.ai_api_topics, local.a_rehearsal_environment) : {
      name = name, value = value
    }]
    secrets = concat(
      [for name in local.backend_secret_names : {
        name      = name
        valueFrom = "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter${name == "JWT_SECRET" ? "${local.v1_source_parameter_prefix}/app/JWT_SECRET" : "${local.backend_parameter_prefix}/${name}"}"
      }],
      [{
        name      = "MYSQL_PASSWORD"
        valueFrom = "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter${local.mysql_app_password_parameter}"
      }],
      [{ name = "REDIS_PASSWORD", valueFrom = module.redis.auth_token_secret_arn }]
    )
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = module.ecs_foundation.backend_log_group_name
        awslogs-region        = var.aws_region
        awslogs-stream-prefix = "a-rehearsal"
      }
    }
  }])

  lifecycle {
    precondition {
      condition     = var.a_rehearsal_backend_image != null
      error_message = "An explicit reviewed Backend digest is required for A rehearsal."
    }
  }

  tags = local.common_tags
}

resource "aws_ecs_service" "a_rehearsal_backend" {
  count           = local.a_rehearsal_enabled ? 1 : 0
  name            = "yeodam-v2-staging-a-rehearsal-backend"
  cluster         = module.ecs_foundation.cluster_arn
  task_definition = aws_ecs_task_definition.a_rehearsal_backend[0].arn
  desired_count   = 1
  launch_type     = "FARGATE"
  depends_on      = [aws_iam_role_policy.a_rehearsal_execution, aws_lb_listener_rule.a_rehearsal_backend]

  network_configuration {
    subnets          = [module.network.private_app_subnet_id]
    security_groups  = [aws_security_group.backend_tasks.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.a_rehearsal_backend[0].arn
    container_name   = "backend"
    container_port   = 8080
  }

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200
  health_check_grace_period_seconds  = 120

  tags = local.common_tags
}
