variable "websocket_enabled" {
  description = "Explicitly prepare the standalone WebSocket service; no changes by default."
  type        = bool
  default     = false
}

variable "websocket_image" {
  description = "Reviewed compatible WebSocket linux/amd64 image, pinned by digest."
  type        = string
  default     = ""
  validation {
    condition     = !var.websocket_enabled || can(regex("^ghcr.io/100-hours-a-week/yeodam-websocket@sha256:[0-9a-f]{64}$", var.websocket_image))
    error_message = "Enabling WebSocket requires its reviewed immutable image."
  }
}

variable "websocket_desired_count" {
  description = "Prepare at zero; set one only after the preserved BE runtime has the shared token/channel."
  type        = number
  default     = 0
  validation {
    condition     = contains([0, 1], var.websocket_desired_count)
    error_message = "This initial verification is scoped to zero or one WebSocket Task."
  }
}

locals {
  websocket_token_path = "/yeodam/v2/production/app/WEBSOCKET_INTERNAL_SERVICE_TOKEN"
  websocket_token_arn  = "arn:aws:ssm:ap-northeast-2:${data.aws_caller_identity.current.account_id}:parameter${local.websocket_token_path}"
  websocket_channel    = "yeodam:production:websocket:events:v1"
}

resource "aws_service_discovery_private_dns_namespace" "websocket" {
  count       = var.websocket_enabled ? 1 : 0
  name        = "services.production.yeodam.internal"
  description = "Private ECS discovery for WebSocket to BE authentication"
  vpc         = data.aws_vpc.production.id
  tags        = local.common_tags
  lifecycle { prevent_destroy = true }
}

resource "aws_service_discovery_service" "websocket_backend" {
  count = var.websocket_enabled ? 1 : 0
  name  = "backend"
  dns_config {
    namespace_id   = aws_service_discovery_private_dns_namespace.websocket[0].id
    routing_policy = "MULTIVALUE"
    dns_records {
      ttl  = 10
      type = "A"
    }
  }
  health_check_custom_config {}
  tags = local.common_tags
}

resource "aws_cloudwatch_log_group" "websocket" {
  count             = var.websocket_enabled ? 1 : 0
  name              = "/yeodam/v2/production/websocket"
  retention_in_days = 7
  tags              = local.common_tags
}

resource "aws_security_group" "websocket" {
  count       = var.websocket_enabled ? 1 : 0
  name_prefix = "${local.name_prefix}-websocket-"
  description = "Standalone WebSocket; ALB entry, private BE and Redis only"
  vpc_id      = data.aws_vpc.production.id
  tags        = merge(local.common_tags, { Name = "${local.name_prefix}-websocket" })
}

resource "aws_vpc_security_group_ingress_rule" "websocket_alb" {
  count                        = var.websocket_enabled ? 1 : 0
  security_group_id            = aws_security_group.websocket[0].id
  referenced_security_group_id = module.app_alb.alb_security_group_id
  ip_protocol                  = "tcp"
  from_port                    = 8081
  to_port                      = 8081
}

resource "aws_vpc_security_group_egress_rule" "alb_websocket" {
  count                        = var.websocket_enabled ? 1 : 0
  security_group_id            = module.app_alb.alb_security_group_id
  referenced_security_group_id = aws_security_group.websocket[0].id
  ip_protocol                  = "tcp"
  from_port                    = 8081
  to_port                      = 8081
}

resource "aws_vpc_security_group_ingress_rule" "backend_websocket" {
  count                        = var.websocket_enabled ? 1 : 0
  security_group_id            = aws_security_group.backend_tasks.id
  referenced_security_group_id = aws_security_group.websocket[0].id
  ip_protocol                  = "tcp"
  from_port                    = 8080
  to_port                      = 8080
}

resource "aws_vpc_security_group_egress_rule" "websocket_backend" {
  count                        = var.websocket_enabled ? 1 : 0
  security_group_id            = aws_security_group.websocket[0].id
  referenced_security_group_id = aws_security_group.backend_tasks.id
  ip_protocol                  = "tcp"
  from_port                    = 8080
  to_port                      = 8080
}

resource "aws_vpc_security_group_ingress_rule" "redis_websocket" {
  count                        = var.websocket_enabled ? 1 : 0
  security_group_id            = module.redis.security_group_id
  referenced_security_group_id = aws_security_group.websocket[0].id
  ip_protocol                  = "tcp"
  from_port                    = 6379
  to_port                      = 6379
}

resource "aws_vpc_security_group_egress_rule" "websocket_redis" {
  count                        = var.websocket_enabled ? 1 : 0
  security_group_id            = aws_security_group.websocket[0].id
  referenced_security_group_id = module.redis.security_group_id
  ip_protocol                  = "tcp"
  from_port                    = 6379
  to_port                      = 6379
}

resource "aws_vpc_security_group_egress_rule" "websocket_https" {
  count             = var.websocket_enabled ? 1 : 0
  security_group_id = aws_security_group.websocket[0].id
  description       = "TLS registry, Parameter, Secret and CloudWatch bootstrap through existing NAT"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}

resource "aws_iam_role" "websocket_execution" {
  count              = var.websocket_enabled ? 1 : 0
  name               = "${local.name_prefix}-websocket-execution"
  assume_role_policy = data.aws_iam_policy_document.backend_task_assume_role.json
  tags               = local.common_tags
}

resource "aws_iam_role_policy" "websocket_execution" {
  count = var.websocket_enabled ? 1 : 0
  name  = "websocket-bootstrap-only"
  role  = aws_iam_role.websocket_execution[0].id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = [module.ecs_foundation.ghcr_credentials_secret_arn, module.redis.auth_token_secret_arn] },
    { Effect = "Allow", Action = ["ssm:GetParameters"], Resource = [local.websocket_token_arn] },
    { Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"], Resource = ["${aws_cloudwatch_log_group.websocket[0].arn}:*"] }
  ] })
}

resource "aws_ecs_task_definition" "websocket" {
  count                    = var.websocket_enabled ? 1 : 0
  family                   = "${local.name_prefix}-websocket"
  skip_destroy             = true
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "256"
  memory                   = "512"
  execution_role_arn       = aws_iam_role.websocket_execution[0].arn
  runtime_platform {
    cpu_architecture        = "X86_64"
    operating_system_family = "LINUX"
  }
  container_definitions = jsonencode([{
    name                  = "websocket", image = var.websocket_image, essential = true
    repositoryCredentials = { credentialsParameter = module.ecs_foundation.ghcr_credentials_secret_arn }
    portMappings          = [{ containerPort = 8081, hostPort = 8081, protocol = "tcp" }]
    environment = [for name, value in {
      BACKEND_INTERNAL_BASE_URL                   = "http://backend.services.production.yeodam.internal:8080"
      WEBSOCKET_ALLOWED_ORIGINS                   = "https://yeodam-2gether.com"
      WEBSOCKET_EVENTS_CHANNEL                    = local.websocket_channel
      WEBSOCKET_MAX_CONNECTIONS                   = "1000"
      REDIS_HOST                                  = module.redis.primary_endpoint_address
      REDIS_PORT                                  = tostring(module.redis.port)
      REDIS_SSL_ENABLED                           = "true"
      SERVER_SHUTDOWN                             = "graceful"
      SPRING_LIFECYCLE_TIMEOUT_PER_SHUTDOWN_PHASE = "30s"
      JAVA_TOOL_OPTIONS                           = "-XX:MaxRAMPercentage=50.0 -XX:InitialRAMPercentage=12.5"
      RELEASE                                     = var.websocket_image
    } : { name = name, value = value }]
    secrets = [
      { name = "WEBSOCKET_INTERNAL_SERVICE_TOKEN", valueFrom = local.websocket_token_arn },
      { name = "REDIS_PASSWORD", valueFrom = module.redis.auth_token_secret_arn }
    ]
    healthCheck = {
      command  = ["CMD-SHELL", "bash -ec 'exec 3<>/dev/tcp/127.0.0.1/8081; printf \"GET /api/actuator/health/liveness HTTP/1.1\\r\\nHost: localhost\\r\\nConnection: close\\r\\n\\r\\n\" >&3; read -r status <&3; [[ \"$status\" == *\" 200 \"* ]]'"]
      interval = 30, timeout = 5, retries = 3, startPeriod = 120
    }
    stopTimeout     = 60
    linuxParameters = { initProcessEnabled = true }
    logConfiguration = { logDriver = "awslogs", options = {
      awslogs-group = aws_cloudwatch_log_group.websocket[0].name, awslogs-region = "ap-northeast-2", awslogs-stream-prefix = "websocket"
    } }
  }])
  tags = local.common_tags
}

resource "aws_lb_target_group" "websocket" {
  count                = var.websocket_enabled ? 1 : 0
  name                 = "yeodam-v2-prod-websocket"
  vpc_id               = data.aws_vpc.production.id
  port                 = 8081
  protocol             = "HTTP"
  target_type          = "ip"
  deregistration_delay = 30
  health_check {
    path                = "/api/actuator/health/readiness"
    matcher             = "200"
    interval            = 15
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 2
  }
  tags = local.common_tags
}

resource "aws_lb_listener_rule" "websocket_internal_block" {
  count        = var.websocket_enabled ? 1 : 0
  listener_arn = module.app_alb.https_listener_arn
  priority     = 5
  action {
    type = "fixed-response"
    fixed_response {
      content_type = "text/plain"
      status_code  = "404"
      message_body = "Not Found"
    }
  }
  condition {
    path_pattern { values = ["/api/internal/websocket", "/api/internal/websocket/*"] }
  }
}

resource "aws_lb_listener_rule" "websocket" {
  count        = var.websocket_enabled ? 1 : 0
  listener_arn = module.app_alb.https_listener_arn
  priority     = 15
  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.websocket[0].arn
  }
  condition {
    host_header { values = ["yeodam-2gether.com"] }
  }
  condition {
    path_pattern { values = ["/api/ws", "/api/ws/*"] }
  }
}

resource "aws_ecs_service" "websocket" {
  count           = var.websocket_enabled ? 1 : 0
  name            = "${local.name_prefix}-websocket"
  cluster         = module.ecs_foundation.cluster_arn
  task_definition = aws_ecs_task_definition.websocket[0].arn
  desired_count   = var.websocket_desired_count
  launch_type     = "FARGATE"
  network_configuration {
    subnets          = [aws_subnet.private_app.id]
    security_groups  = [aws_security_group.websocket[0].id]
    assign_public_ip = false
  }
  load_balancer {
    target_group_arn = aws_lb_target_group.websocket[0].arn
    container_name   = "websocket"
    container_port   = 8081
  }
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }
  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200
  health_check_grace_period_seconds  = 180
  depends_on                         = [aws_lb_listener_rule.websocket, aws_iam_role_policy.websocket_execution]
  tags                               = local.common_tags
}
