locals {
  staging_origin           = "https://staging.yeodam-2gether.com"
  backend_parameter_prefix = "/yeodam/v2/staging/app"

  backend_secret_names = toset([
    "MYSQL_PASSWORD",
    "KAKAO_CLIENT_SECRET",
    "JWT_SECRET",
    "AI_SERVER_API_KEY",
  ])

  backend_base_environment = {
    SPRING_PROFILES_ACTIVE      = "prod"
    AWS_REGION                  = var.aws_region
    ATTACHMENT_S3_BUCKET        = module.app_storage.bucket_name
    FRONTEND_ORIGIN             = local.staging_origin
    FRONTEND_OAUTH_CALLBACK_URI = "${local.staging_origin}/auth/callback"
    KAKAO_REDIRECT_URI          = "${local.staging_origin}/api/auth/kakao/callback"
    JWT_ISSUER                  = local.staging_origin
    REDIS_HOST                  = module.redis.primary_endpoint_address
    REDIS_PORT                  = tostring(module.redis.port)
    REDIS_SSL_ENABLED           = "true"
    AUTH_SESSION_KEY_PREFIX     = "yeodam:v2:staging:auth:"
  }
}

resource "aws_security_group" "frontend_tasks" {
  name_prefix = "yeodam-v2-staging-frontend-"
  description = "Frontend ECS tasks behind the staging ALB"
  vpc_id      = module.network.vpc_id
  tags        = merge(local.common_tags, { Name = "yeodam-v2-staging-frontend-sg" })
}

resource "aws_vpc_security_group_ingress_rule" "frontend_alb" {
  security_group_id            = aws_security_group.frontend_tasks.id
  referenced_security_group_id = module.app_alb.alb_security_group_id
  description                  = "Frontend requests from staging ALB"
  ip_protocol                  = "tcp"
  from_port                    = 3000
  to_port                      = 3000
}

resource "aws_vpc_security_group_egress_rule" "frontend_https" {
  security_group_id = aws_security_group.frontend_tasks.id
  description       = "SSR API and external HTTPS through NAT"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}

resource "aws_vpc_security_group_ingress_rule" "backend_alb" {
  security_group_id            = aws_security_group.backend_tasks.id
  referenced_security_group_id = module.app_alb.alb_security_group_id
  description                  = "Backend API requests from staging ALB"
  ip_protocol                  = "tcp"
  from_port                    = 8080
  to_port                      = 8080
}

resource "aws_iam_role_policy" "backend_runtime_parameters" {
  name = "yeodam-v2-staging-backend-runtime-parameters"
  role = aws_iam_role.backend_execution.id

  policy = data.aws_iam_policy_document.backend_runtime_parameters.json
}

data "aws_iam_policy_document" "backend_runtime_parameters" {
  statement {
    sid       = "ReadStagingBackendRuntimeSecrets"
    actions   = ["ssm:GetParameters"]
    resources = ["arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter${local.backend_parameter_prefix}/*"]
  }
}

resource "aws_ecs_task_definition" "frontend" {
  count = var.frontend_image == null ? 0 : 1

  family                   = "yeodam-v2-staging-frontend"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "512"
  memory                   = "1024"
  execution_role_arn       = module.ecs_foundation.task_execution_role_arn

  container_definitions = jsonencode([{
    name      = "frontend"
    image     = var.frontend_image
    essential = true
    repositoryCredentials = {
      credentialsParameter = module.ecs_foundation.ghcr_credentials_secret_arn
    }
    portMappings = [{ containerPort = 3000, hostPort = 3000, protocol = "tcp" }]
    environment  = [{ name = "SERVER_API_BASE_URL", value = "${local.staging_origin}/api" }]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = module.ecs_foundation.frontend_log_group_name
        awslogs-region        = var.aws_region
        awslogs-stream-prefix = "frontend"
      }
    }
  }])

  tags = local.common_tags
}

resource "aws_ecs_task_definition" "backend" {
  count = var.backend_image == null ? 0 : 1

  family                   = "yeodam-v2-staging-backend"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "1024"
  memory                   = "2048"
  execution_role_arn       = aws_iam_role.backend_execution.arn
  task_role_arn            = aws_iam_role.backend_task.arn

  container_definitions = jsonencode([{
    name      = "backend"
    image     = var.backend_image
    essential = true
    repositoryCredentials = {
      credentialsParameter = module.ecs_foundation.ghcr_credentials_secret_arn
    }
    portMappings = [{ containerPort = 8080, hostPort = 8080, protocol = "tcp" }]
    environment = [
      for name, value in merge(var.backend_environment, local.backend_base_environment) :
      { name = name, value = value }
    ]
    secrets = concat(
      [for name in local.backend_secret_names : {
        name      = name
        valueFrom = "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter${local.backend_parameter_prefix}/${name}"
      }],
      [{ name = "REDIS_PASSWORD", valueFrom = module.redis.auth_token_secret_arn }]
    )
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = module.ecs_foundation.backend_log_group_name
        awslogs-region        = var.aws_region
        awslogs-stream-prefix = "backend"
      }
    }
  }])

  lifecycle {
    precondition {
      condition = alltrue([
        for name in ["MYSQL_URL", "MYSQL_USERNAME", "KAKAO_CLIENT_ID"] :
        try(length(trimspace(var.backend_environment[name])) > 0, false)
      ])
      error_message = "backend_environment needs MYSQL_URL, MYSQL_USERNAME and KAKAO_CLIENT_ID before a Backend task can be enabled."
    }
  }

  tags = local.common_tags
}

resource "aws_ecs_service" "frontend" {
  count = var.frontend_image == null ? 0 : 1

  depends_on = [module.ecs_foundation]

  name            = "yeodam-v2-staging-frontend"
  cluster         = module.ecs_foundation.cluster_arn
  task_definition = aws_ecs_task_definition.frontend[0].arn
  desired_count   = var.frontend_desired_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = [module.network.private_app_subnet_id]
    security_groups  = [aws_security_group.frontend_tasks.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = module.app_alb.frontend_target_group_arn
    container_name   = "frontend"
    container_port   = 3000
  }

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200
  health_check_grace_period_seconds  = 90

  lifecycle {
    # Subsequent image revisions are deployed by the staging CD workflow.
    ignore_changes = [task_definition]
  }

  tags = local.common_tags
}

resource "aws_ecs_service" "backend" {
  count = var.backend_image == null ? 0 : 1

  depends_on = [aws_iam_role_policy.backend_execution, aws_iam_role_policy.backend_runtime_parameters]

  name            = "yeodam-v2-staging-backend"
  cluster         = module.ecs_foundation.cluster_arn
  task_definition = aws_ecs_task_definition.backend[0].arn
  desired_count   = var.backend_desired_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = [module.network.private_app_subnet_id]
    security_groups  = [aws_security_group.backend_tasks.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = module.app_alb.backend_target_group_arn
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

# Publish the shared staging address only when a frontend task can be started.
resource "aws_route53_record" "app_staging" {
  count = var.frontend_image == null ? 0 : 1

  zone_id = data.aws_route53_zone.public.zone_id
  name    = "staging.yeodam-2gether.com"
  type    = "A"

  alias {
    name                   = module.app_alb.alb_dns_name
    zone_id                = module.app_alb.alb_zone_id
    evaluate_target_health = false
  }
}
