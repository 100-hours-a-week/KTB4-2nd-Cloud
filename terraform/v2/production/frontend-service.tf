locals {
  production_frontend_manifest = jsondecode(file("${path.module}/../../../deploy/v2/image-manifest.json")).components.frontend
  production_frontend_image    = "ghcr.io/100-hours-a-week/yeodam-frontend@${local.production_frontend_manifest.digest}"
}

resource "aws_security_group" "frontend_tasks" {
  name_prefix = "${local.name_prefix}-frontend-"
  description = "Production frontend tasks behind the application ALB"
  vpc_id      = data.aws_vpc.production.id
  tags        = merge(local.common_tags, { Name = "${local.name_prefix}-frontend-sg" })
}

resource "aws_vpc_security_group_ingress_rule" "frontend_alb" {
  security_group_id            = aws_security_group.frontend_tasks.id
  referenced_security_group_id = module.app_alb.alb_security_group_id
  description                  = "Only production application ALB to frontend HTTP"
  ip_protocol                  = "tcp"
  from_port                    = 3000
  to_port                      = 3000
}

resource "aws_vpc_security_group_egress_rule" "frontend_https" {
  security_group_id = aws_security_group.frontend_tasks.id
  description       = "Canonical production HTTPS API and external HTTPS through existing NAT"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}

resource "aws_ecs_task_definition" "frontend" {
  skip_destroy             = true
  family                   = "${local.name_prefix}-frontend"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "512"
  memory                   = "1024"
  execution_role_arn       = module.ecs_foundation.task_execution_role_arn
  runtime_platform {
    cpu_architecture        = "X86_64"
    operating_system_family = "LINUX"
  }
  container_definitions = jsonencode([{
    name                  = "frontend"
    image                 = local.production_frontend_image
    essential             = true
    repositoryCredentials = { credentialsParameter = module.ecs_foundation.ghcr_credentials_secret_arn }
    portMappings          = [{ containerPort = 3000, hostPort = 3000, protocol = "tcp" }]
    environment = [
      { name = "HOSTNAME", value = "0.0.0.0" },
      { name = "SERVER_API_BASE_URL", value = "https://yeodam-2gether.com/api" },
      { name = "RELEASE", value = local.production_frontend_manifest.sourceSha },
    ]
    healthCheck = {
      command  = ["CMD", "node", "-e", "fetch('http://127.0.0.1:3000/api/health',{signal:AbortSignal.timeout(4000)}).then(r=>{if(!r.ok)process.exit(1)}).catch(()=>process.exit(1))"]
      interval = 30, timeout = 5, retries = 3, startPeriod = 60
    }
    linuxParameters = { initProcessEnabled = true }
    stopTimeout     = 60
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = module.ecs_foundation.frontend_log_group_name
        awslogs-region        = "ap-northeast-2"
        awslogs-stream-prefix = "frontend"
      }
    }
  }])
  tags = local.common_tags
}

resource "aws_ecs_service" "frontend" {
  name            = "${local.name_prefix}-frontend"
  cluster         = module.ecs_foundation.cluster_arn
  task_definition = aws_ecs_task_definition.frontend.arn
  desired_count   = 1
  launch_type     = "FARGATE"
  network_configuration {
    subnets          = [aws_subnet.private_app.id]
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
  health_check_grace_period_seconds  = 120
  depends_on                         = [module.app_alb, module.ecs_foundation, aws_route.private_internet, aws_route_table_association.private_app]
  lifecycle {
    # Application revisions will be promoted by CD; Terraform owns service infrastructure.
    ignore_changes = [task_definition]
  }
  tags = local.common_tags
}

output "frontend_service_name" { value = aws_ecs_service.frontend.name }
output "frontend_task_definition_arn" { value = aws_ecs_task_definition.frontend.arn }
output "frontend_image" { value = local.production_frontend_image }
