locals {
  production_backend_manifest = local.production_release_manifest.components.backend
  production_backend_image    = "ghcr.io/100-hours-a-week/yeodam-backend@${local.production_backend_manifest.digest}"
  backend_origin              = "https://yeodam-2gether.com"
  backend_parameters = {
    MYSQL_PASSWORD                    = local.mysql_app_password_parameter
    KAKAO_CLIENT_ID                   = "/yeodam/v2/production/app/KAKAO_CLIENT_ID"
    KAKAO_CLIENT_SECRET               = "/yeodam/v2/production/app/KAKAO_CLIENT_SECRET"
    JWT_SECRET                        = "/yeodam/v2/production/app/JWT_SECRET"
    AI_SERVER_API_KEY                 = local.production_ai_api_key_parameter
    DATA_GO_KR_SERVICE_KEY            = "/yeodam/v2/production/app/DATA_GO_KR_SERVICE_KEY"
    DATA_GO_KR_LEGAL_DISTRICT_API_URL = "/yeodam/v2/production/app/DATA_GO_KR_LEGAL_DISTRICT_API_URL"
    DATA_GO_KR_TIMEOUT                = "/yeodam/v2/production/app/DATA_GO_KR_TIMEOUT"
  }
  backend_environment = {
    SPRING_PROFILES_ACTIVE              = "prod,internal-metrics"
    MANAGEMENT_SERVER_ADDRESS           = "0.0.0.0"
    SPRING_JPA_HIBERNATE_DDL_AUTO       = "validate"
    SPRING_FLYWAY_ENABLED               = "true"
    MYSQL_URL                           = "jdbc:mysql://${aws_route53_record.mysql_predeploy.fqdn}:3306/yeodam?serverTimezone=Asia/Seoul&characterEncoding=utf8"
    MYSQL_USERNAME                      = "yeodam_app"
    REDIS_HOST                          = module.redis.primary_endpoint_address
    REDIS_PORT                          = tostring(module.redis.port)
    REDIS_SSL_ENABLED                   = "true"
    AUTH_SESSION_KEY_PREFIX             = "yeodam:v2:production:auth:"
    AI_SERVER_BASE_URL                  = "http://${aws_route53_record.ai_api_private.fqdn}"
    AI_EC2_INSTANCE_ID                  = data.aws_instance.shared_gpu.id # Legacy constructor only; no EC2 action granted.
    KAFKA_BOOTSTRAP_SERVERS             = "${aws_route53_record.kafka_private.fqdn}:9092"
    APP_TRIP_RESULT_LISTENER_ENABLED    = "true"
    STORY_RESULT_LISTENER_ENABLED       = "true"
    APP_OUTBOX_RECEIPT_LISTENER_ENABLED = "true"
    KAFKA_TRIP_RESULT_CONSUMER_GROUP    = "prod.yeodam-trip-results"
    KAFKA_STORY_RESULT_CONSUMER_GROUP   = "prod.yeodam-story-results"
    KAFKA_OUTBOX_RECEIPT_CONSUMER_GROUP = "prod.yeodam-outbox-receipts"
    FRONTEND_ORIGIN                     = local.backend_origin
    FRONTEND_OAUTH_CALLBACK_URI         = "${local.backend_origin}/auth/callback"
    KAKAO_REDIRECT_URI                  = "${local.backend_origin}/api/auth/kakao/callback"
    JWT_ISSUER                          = local.backend_origin
    AWS_REGION                          = "ap-northeast-2"
    ATTACHMENT_S3_BUCKET                = data.aws_s3_bucket.production_photos.id
    SENTRY_ENVIRONMENT                  = "production"
    RELEASE                             = local.production_backend_manifest.sourceSha
  }
}

resource "aws_vpc_security_group_ingress_rule" "backend_alb" {
  security_group_id            = aws_security_group.backend_tasks.id
  referenced_security_group_id = module.app_alb.alb_security_group_id
  description                  = "Only production application ALB to backend HTTP"
  ip_protocol                  = "tcp"
  from_port                    = 8080
  to_port                      = 8080
}

resource "aws_iam_role_policy" "backend_runtime_parameters" {
  name = "production-backend-runtime-parameters"
  role = aws_iam_role.backend_execution.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Action = ["ssm:GetParameters"], Resource = [for path in values(local.backend_parameters) : "arn:aws:ssm:ap-northeast-2:${data.aws_caller_identity.current.account_id}:parameter${path}"]
  }] })
}

resource "aws_iam_role" "backend_task" {
  name               = "${local.name_prefix}-backend-task"
  assume_role_policy = data.aws_iam_policy_document.backend_task_assume_role.json
  tags               = local.common_tags
}

resource "aws_iam_role_policy" "backend_photo_storage" {
  name = "production-backend-photo-storage"
  role = aws_iam_role.backend_task.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["s3:GetBucketLocation"], Resource = data.aws_s3_bucket.production_photos.arn },
    { Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject", "s3:PutObjectTagging", "s3:DeleteObject"], Resource = ["${data.aws_s3_bucket.production_photos.arn}/trip-uploads/*", "${data.aws_s3_bucket.production_photos.arn}/trip-additions/*"] },
    { Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject", "s3:PutObjectTagging"], Resource = "${data.aws_s3_bucket.production_photos.arn}/trip-downloads/*" }
  ] })
}

resource "aws_ecs_task_definition" "backend" {
  skip_destroy             = true
  family                   = "${local.name_prefix}-backend"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "1024"
  memory                   = "2048"
  execution_role_arn       = aws_iam_role.backend_execution.arn
  task_role_arn            = aws_iam_role.backend_task.arn
  runtime_platform {
    cpu_architecture        = "X86_64"
    operating_system_family = "LINUX"
  }
  container_definitions = jsonencode([{
    name                  = "backend", image = local.production_backend_image, essential = true
    repositoryCredentials = { credentialsParameter = module.ecs_foundation.ghcr_credentials_secret_arn }
    portMappings = [
      { containerPort = 8080, hostPort = 8080, protocol = "tcp" },
      { containerPort = 9091, hostPort = 9091, protocol = "tcp" },
    ]
    environment = [for name, value in merge(local.backend_environment, local.ai_api_topics) : { name = name, value = value }]
    secrets = concat([for name, path in local.backend_parameters : {
      name = name, valueFrom = "arn:aws:ssm:ap-northeast-2:${data.aws_caller_identity.current.account_id}:parameter${path}"
    }], [{ name = "REDIS_PASSWORD", valueFrom = module.redis.auth_token_secret_arn }])
    healthCheck = {
      command  = ["CMD-SHELL", "curl --fail --silent http://localhost:8080/api/actuator/health >/dev/null"]
      interval = 30, timeout = 5, retries = 3, startPeriod = 120
    }
    linuxParameters = { initProcessEnabled = true }
    stopTimeout     = 120
    logConfiguration = {
      logDriver = "awslogs"
      options   = { awslogs-group = module.ecs_foundation.backend_log_group_name, awslogs-region = "ap-northeast-2", awslogs-stream-prefix = "backend" }
    }
  }])
  tags = local.common_tags
}

resource "aws_ecs_service" "backend" {
  name            = "${local.name_prefix}-backend"
  cluster         = module.ecs_foundation.cluster_arn
  task_definition = aws_ecs_task_definition.backend.arn
  desired_count   = 1
  launch_type     = "FARGATE"
  network_configuration {
    subnets          = [aws_subnet.private_app.id]
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
  health_check_grace_period_seconds  = 180
  depends_on                         = [aws_iam_role_policy.backend_execution, aws_iam_role_policy.backend_runtime_parameters, aws_iam_role_policy.backend_photo_storage, module.app_alb]
  lifecycle { ignore_changes = [task_definition] }
  tags = local.common_tags
}

output "backend_service_name" { value = aws_ecs_service.backend.name }
output "backend_task_role_arn" { value = aws_iam_role.backend_task.arn }
output "backend_image" { value = local.production_backend_image }

output "backend_task_definition_arn" { value = aws_ecs_task_definition.backend.arn }
