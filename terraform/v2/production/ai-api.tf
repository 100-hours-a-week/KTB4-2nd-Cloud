locals {
  ai_api_name = "yeodam-v2-production-ai-api"
  ai_api_parameters = {
    API_KEY           = local.production_ai_api_key_parameter
    REDIS_URL         = local.ai_redis_parameter
    EMBEDDING_API_KEY = "/yeodam/v2/staging/ai/EMBEDDING_API_KEY" # Same model server/key as the production runner.
    VLM_API_KEY       = "/yeodam/v2/production/ai/VLM_API_KEY"
    VLM_BASE_URL      = "/yeodam/v2/production/ai/VLM_BASE_URL"
  }
  ai_api_environment = {
    JOBS_ENV                    = "production"
    JOBS_PROCESS_HANDLER        = "app.jobs.process_handler:run"
    JOBS_STORY_HANDLER          = "app.story.handler:run"
    JOBS_FAKE_PIPELINE          = "false"
    FAKE_PIPELINE               = "false"
    SEARCH_ENABLED              = "true"
    IMAGE_SOURCE                = "s3"
    S3_BUCKET                   = data.aws_s3_bucket.production_photos.id
    AWS_REGION                  = "ap-northeast-2"
    EMBED_MODEL                 = "qwen3-vl-embedding-2b"
    EMBEDDING_BASE_URL          = "http://${aws_route53_record.shared_gpu_production.fqdn}:8000/v1"
    EMBEDDING_SERVED_MODEL_NAME = "qwen3-vl-embedding-2b"
    QDRANT_URL                  = "http://${aws_route53_record.shared_gpu_production.fqdn}:6335"
    KAFKA_BOOTSTRAP_SERVERS     = "${aws_route53_record.kafka_private.fqdn}:9092"
    KAFKA_SECURITY_PROTOCOL     = "PLAINTEXT"
    VLM_MODEL                   = "gemma-4"
    # AI's deployment handoff values; this is a shared GPU call limit, not story concurrency.
    STORY_MAX_TOKENS        = "2048"
    GPU_GENERATION_INFLIGHT = "8"
    RELEASE                 = local.production_ai_manifest.sourceSha
  }
  ai_api_topics = {
    for pair in setproduct(["TRIP_CREATE", "TRIP_UPDATE", "STORY_CREATE"], ["REQUEST", "RESULT"]) :
    "KAFKA_${pair[0]}_${pair[1]}_TOPIC" => "prod.yeodam.${replace(lower(pair[0]), "_", "-")}.${lower(pair[1])}"
  }
}

# Credentials and the mutable RunPod URL are seeded outside Terraform state.
data "aws_iam_policy_document" "ai_task_assume_role" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_cloudwatch_log_group" "ai_api" {
  name              = "/yeodam/v2/production/ai-api"
  retention_in_days = 30
  tags              = local.common_tags
}

resource "aws_iam_role" "ai_api_execution" {
  name               = "${local.ai_api_name}-execution"
  assume_role_policy = data.aws_iam_policy_document.ai_task_assume_role.json
  tags               = local.common_tags
}

resource "aws_iam_role_policy" "ai_api_execution" {
  name = "ai-api-runtime"
  role = aws_iam_role.ai_api_execution.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["ssm:GetParameters"], Resource = [for path in values(local.ai_api_parameters) : "arn:aws:ssm:ap-northeast-2:${data.aws_caller_identity.current.account_id}:parameter${path}"] },
    { Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = module.ecs_foundation.ghcr_credentials_secret_arn },
    { Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"], Resource = "${aws_cloudwatch_log_group.ai_api.arn}:*" }
  ] })
}

resource "aws_iam_role" "ai_api_task" {
  name               = "${local.ai_api_name}-task"
  assume_role_policy = data.aws_iam_policy_document.ai_task_assume_role.json
  tags               = local.common_tags
}

resource "aws_iam_role_policy" "ai_api_photos" {
  name = "ai-api-derived-photos"
  role = aws_iam_role.ai_api_task.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Action = ["s3:GetObject"], Resource = [
      "${data.aws_s3_bucket.production_photos.arn}/trip-uploads/*/derived/*",
      "${data.aws_s3_bucket.production_photos.arn}/trip-additions/*/derived/*",
    ]
  }] })
}

resource "aws_ecs_task_definition" "ai_api" {
  skip_destroy             = true # Keep a healthy prior revision available during rolling updates.
  family                   = local.ai_api_name
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "512"
  memory                   = "1024"
  execution_role_arn       = aws_iam_role.ai_api_execution.arn
  task_role_arn            = aws_iam_role.ai_api_task.arn
  runtime_platform {
    cpu_architecture        = "X86_64"
    operating_system_family = "LINUX"
  }
  container_definitions = jsonencode([{
    name                  = "ai-api"
    image                 = local.production_runner_image
    essential             = true
    repositoryCredentials = { credentialsParameter = module.ecs_foundation.ghcr_credentials_secret_arn }
    portMappings          = [{ name = "http", containerPort = 8000, hostPort = 8000, protocol = "tcp" }]
    environment           = [for name, value in merge(local.ai_api_environment, local.ai_api_topics) : { name = name, value = value }]
    secrets = [for name, path in local.ai_api_parameters : {
      name      = name
      valueFrom = "arn:aws:ssm:ap-northeast-2:${data.aws_caller_identity.current.account_id}:parameter${path}"
    }]
    healthCheck = {
      command  = ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=5)"]
      interval = 30, timeout = 10, retries = 3, startPeriod = 120
    }
    linuxParameters = { initProcessEnabled = true }
    stopTimeout     = 120
    logConfiguration = {
      logDriver = "awslogs"
      options   = { awslogs-group = aws_cloudwatch_log_group.ai_api.name, awslogs-region = "ap-northeast-2", awslogs-stream-prefix = "ai-api" }
    }
  }])
  tags = local.common_tags
}

resource "aws_security_group" "ai_internal_alb" {
  name_prefix = "${local.ai_api_name}-alb-"
  description = "Private BE to AI API load balancer"
  vpc_id      = data.aws_vpc.production.id
  tags        = merge(local.common_tags, { Name = "${local.ai_api_name}-alb-sg" })
}

resource "aws_vpc_security_group_ingress_rule" "ai_alb_backend" {
  security_group_id            = aws_security_group.ai_internal_alb.id
  referenced_security_group_id = aws_security_group.backend_tasks.id
  ip_protocol                  = "tcp"
  from_port                    = 80
  to_port                      = 80
  description                  = "Backend HTTP requests to internal AI ALB"
}

resource "aws_vpc_security_group_egress_rule" "backend_ai_alb" {
  security_group_id            = aws_security_group.backend_tasks.id
  referenced_security_group_id = aws_security_group.ai_internal_alb.id
  ip_protocol                  = "tcp"
  from_port                    = 80
  to_port                      = 80
  description                  = "Backend to internal AI ALB"
}

resource "aws_vpc_security_group_egress_rule" "ai_alb_tasks" {
  security_group_id            = aws_security_group.ai_internal_alb.id
  referenced_security_group_id = aws_security_group.ai_kafka_clients.id
  ip_protocol                  = "tcp"
  from_port                    = 8000
  to_port                      = 8000
  description                  = "Internal ALB health checks and HTTP to AI tasks"
}

resource "aws_vpc_security_group_ingress_rule" "ai_api_alb" {
  security_group_id            = aws_security_group.ai_kafka_clients.id
  referenced_security_group_id = aws_security_group.ai_internal_alb.id
  ip_protocol                  = "tcp"
  from_port                    = 8000
  to_port                      = 8000
  description                  = "Only the internal ALB can reach the AI HTTP port"
}

resource "aws_lb" "ai_internal" {
  name               = "yeodam-v2-prod-ai-api"
  internal           = true
  load_balancer_type = "application"
  security_groups    = [aws_security_group.ai_internal_alb.id]
  # Two private AZs for the ALB; tasks remain in private_app with the GPU return route.
  subnets                    = [aws_subnet.private_app.id, aws_subnet.private_ai_alb_secondary.id]
  idle_timeout               = 120
  drop_invalid_header_fields = true
  tags                       = local.common_tags
}

resource "aws_lb_target_group" "ai_api" {
  name                 = "yeodam-v2-prod-ai-api"
  port                 = 8000
  protocol             = "HTTP"
  target_type          = "ip"
  vpc_id               = data.aws_vpc.production.id
  deregistration_delay = 30
  health_check {
    path                = "/health"
    matcher             = "200"
    interval            = 15
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }
  tags = local.common_tags
}

resource "aws_lb_listener" "ai_api" {
  load_balancer_arn = aws_lb.ai_internal.arn
  port              = 80
  protocol          = "HTTP"
  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.ai_api.arn
  }
}

resource "aws_route53_record" "ai_api_private" {
  zone_id = aws_route53_zone.production_private.zone_id
  name    = "ai-api.production.yeodam.internal"
  type    = "A"
  alias {
    name                   = aws_lb.ai_internal.dns_name
    zone_id                = aws_lb.ai_internal.zone_id
    evaluate_target_health = false
  }
}

resource "aws_ecs_service" "ai_api" {
  name            = local.ai_api_name
  cluster         = module.ecs_foundation.cluster_arn
  task_definition = aws_ecs_task_definition.ai_api.arn
  desired_count   = 1
  launch_type     = "FARGATE"
  network_configuration {
    subnets          = [aws_subnet.private_app.id]
    security_groups  = [aws_security_group.ai_kafka_clients.id]
    assign_public_ip = false
  }
  load_balancer {
    target_group_arn = aws_lb_target_group.ai_api.arn
    container_name   = "ai-api"
    container_port   = 8000
  }
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }
  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200
  health_check_grace_period_seconds  = 180
  depends_on                         = [aws_lb_listener.ai_api, aws_iam_role_policy.ai_api_execution, aws_iam_role_policy.ai_api_photos, aws_route_table_association.private_app, aws_route.private_internet]
  tags                               = local.common_tags
  lifecycle {
    # Application revisions are promoted by CD; Terraform owns the service infrastructure.
    ignore_changes = [task_definition]
  }
}

output "ai_api_base_url" { value = "http://${aws_route53_record.ai_api_private.fqdn}" }
output "ai_api_service_name" { value = aws_ecs_service.ai_api.name }
output "ai_api_target_group_arn" { value = aws_lb_target_group.ai_api.arn }
