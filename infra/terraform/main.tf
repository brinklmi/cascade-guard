# CascadeGuard MCP Proxy Middleware - Terraform Main Configuration
# VPC-Private deployment on ECS Fargate with PrivateLink endpoint

terraform {
  required_version = ">= 1.5.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

# Data source: VPC CIDR for security group rules
data "aws_vpc" "selected" {
  id = var.vpc_id
}

# Security Group - VPC internal only, no public exposure
resource "aws_security_group" "proxy" {
  name_prefix = "cascadeguard-proxy-"
  description = "CascadeGuard MCP Proxy - VPC internal traffic only"
  vpc_id      = var.vpc_id

  ingress {
    description = "MCP proxy port from VPC"
    from_port   = var.proxy_port
    to_port     = var.proxy_port
    protocol    = "tcp"
    cidr_blocks = [data.aws_vpc.selected.cidr_block]
  }

  egress {
    description = "HTTPS outbound for target servers"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(var.tags, { Name = "cascadeguard-mcp-proxy-sg" })
}

# CloudWatch Log Group
resource "aws_cloudwatch_log_group" "proxy" {
  name              = "/ecs/cascadeguard-mcp-proxy"
  retention_in_days = var.log_retention_days
  tags              = var.tags
}

# ECS Cluster
resource "aws_ecs_cluster" "proxy" {
  name = "cascadeguard-mcp-proxy"

  setting {
    name  = "containerInsights"
    value = "enabled"
  }

  tags = var.tags
}

# IAM Role - Task Execution (pull images, write logs)
resource "aws_iam_role" "task_execution" {
  name_prefix = "cascadeguard-exec-"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action = "sts:AssumeRole"
      Effect = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
    }]
  })

  tags = var.tags
}

resource "aws_iam_role_policy_attachment" "task_execution" {
  role       = aws_iam_role.task_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# IAM Role - Task Runtime (CloudWatch metrics, secrets)
resource "aws_iam_role" "task" {
  name_prefix = "cascadeguard-task-"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action = "sts:AssumeRole"
      Effect = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
    }]
  })

  tags = var.tags
}

resource "aws_iam_role_policy" "task_cloudwatch" {
  name = "cloudwatch-metrics"
  role = aws_iam_role.task.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "cloudwatch:PutMetricData",
        "logs:CreateLogGroup",
        "logs:CreateLogStream",
        "logs:PutLogEvents"
      ]
      Resource = "*"
    }]
  })
}

# ECS Task Definition
resource "aws_ecs_task_definition" "proxy" {
  family                   = "cascadeguard-mcp-proxy"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = var.cpu_units
  memory                   = var.memory_mb
  execution_role_arn       = aws_iam_role.task_execution.arn
  task_role_arn            = aws_iam_role.task.arn

  container_definitions = jsonencode([{
    name      = "mcp-proxy"
    image     = var.container_image
    essential = true

    portMappings = [{
      containerPort = var.proxy_port
      protocol      = "tcp"
    }]

    environment = [
      { name = "CASCADEGUARD_TRANSPORT_PORT", value = tostring(var.proxy_port) },
      { name = "CASCADEGUARD_TRANSPORT_TYPE", value = "sse" },
      { name = "CASCADEGUARD_ENGINE_MAX_VELOCITY", value = tostring(var.max_velocity) },
      { name = "CASCADEGUARD_ENGINE_DEPTH_LIMIT", value = tostring(var.depth_limit) },
      { name = "CASCADEGUARD_ENGINE_FANOUT_LIMIT", value = tostring(var.fanout_limit) },
      { name = "CASCADEGUARD_ENGINE_PRESERVATION_THRESHOLD", value = tostring(var.preservation_threshold) },
      { name = "CASCADEGUARD_ENGINE_TOKEN_BUDGET", value = tostring(var.token_budget) },
      { name = "CASCADEGUARD_SHUTDOWN_DRAIN_TIMEOUT", value = tostring(var.drain_timeout_seconds) },
    ]

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.proxy.name
        "awslogs-region"        = data.aws_region.current.name
        "awslogs-stream-prefix" = "proxy"
      }
    }

    healthCheck = {
      command     = ["CMD-SHELL", "curl -f http://localhost:${var.proxy_port}/health || exit 1"]
      interval    = 30
      timeout     = 5
      retries     = 3
      startPeriod = 10
    }

    stopTimeout = var.drain_timeout_seconds
  }])

  tags = var.tags
}

data "aws_region" "current" {}

# Network Load Balancer (internal only)
resource "aws_lb" "proxy" {
  name               = "cascadeguard-proxy-nlb"
  internal           = true
  load_balancer_type = "network"
  subnets            = var.subnet_ids

  tags = var.tags
}

# NLB Target Group
resource "aws_lb_target_group" "proxy" {
  name        = "cascadeguard-proxy-tg"
  port        = var.proxy_port
  protocol    = "TCP"
  target_type = "ip"
  vpc_id      = var.vpc_id

  health_check {
    protocol            = "HTTP"
    path                = "/health"
    port                = var.proxy_port
    healthy_threshold   = 2
    unhealthy_threshold = 2
  }

  tags = var.tags
}

# NLB Listener
resource "aws_lb_listener" "proxy" {
  load_balancer_arn = aws_lb.proxy.arn
  port              = var.proxy_port
  protocol          = "TCP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.proxy.arn
  }
}

# ECS Service
resource "aws_ecs_service" "proxy" {
  name            = "cascadeguard-mcp-proxy"
  cluster         = aws_ecs_cluster.proxy.id
  task_definition = aws_ecs_task_definition.proxy.arn
  desired_count   = var.desired_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = var.subnet_ids
    security_groups  = [aws_security_group.proxy.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.proxy.arn
    container_name   = "mcp-proxy"
    container_port   = var.proxy_port
  }

  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200
  health_check_grace_period_seconds  = 30

  depends_on = [aws_lb_listener.proxy]

  tags = var.tags
}

# VPC Endpoint Service (PrivateLink)
resource "aws_vpc_endpoint_service" "proxy" {
  acceptance_required        = true
  network_load_balancer_arns = [aws_lb.proxy.arn]

  tags = merge(var.tags, { Name = "cascadeguard-mcp-proxy-endpoint" })
}
