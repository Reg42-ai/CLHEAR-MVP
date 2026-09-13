# Self-hosted Langfuse (HLD v2 §3 "Evals": Langfuse self-hosted + per-layer golden
# sets in clhear-evals/). Maintainers browse eval traces and score history here;
# the public evals dashboard reads only the published summary table
# (app/clhear/platform/gates.py publish_summary → /evals/summary), never Langfuse.
#
# Langfuse v2 on Fargate behind the shared ALB at langfuse_hostname, database
# `langfuse` on the CLHEAR Aurora cluster (own role). Reachable only through
# Cognito-authenticated maintainers (Langfuse's own accounts, sign-up disabled).
# Enabled with langfuse_enabled = true once the VPC, Aurora and the ALB are known.

variable "langfuse_enabled" {
  type    = bool
  default = false
}

variable "langfuse_hostname" {
  type    = string
  default = "evals.clhear.reg42.ai"
}

variable "langfuse_image" {
  type    = string
  default = "langfuse/langfuse:2"
}

locals {
  deploy_langfuse = var.langfuse_enabled && local.have_network && local.deploy_aurora
  langfuse_url    = local.deploy_langfuse ? "https://${var.langfuse_hostname}" : ""
}

resource "random_password" "langfuse_db" {
  count   = local.deploy_langfuse ? 1 : 0
  length  = 32
  special = false
}

resource "random_password" "langfuse_nextauth" {
  count   = local.deploy_langfuse ? 1 : 0
  length  = 48
  special = false
}

resource "random_password" "langfuse_salt" {
  count   = local.deploy_langfuse ? 1 : 0
  length  = 32
  special = false
}

# Created by the operator once (psql on the Aurora cluster):
#   CREATE ROLE langfuse LOGIN PASSWORD '<value of /clhear/LANGFUSE_DB_PASSWORD>';
#   CREATE DATABASE langfuse OWNER langfuse;
resource "aws_ssm_parameter" "langfuse_db_password" {
  name        = "/clhear/LANGFUSE_DB_PASSWORD"
  type        = "SecureString"
  value       = local.deploy_langfuse ? random_password.langfuse_db[0].result : "CHANGEME"
  description = "Postgres password of the `langfuse` role on the CLHEAR Aurora cluster"
  lifecycle {
    ignore_changes = [value]
  }
}

resource "aws_ssm_parameter" "langfuse_nextauth_secret" {
  name  = "/clhear/LANGFUSE_NEXTAUTH_SECRET"
  type  = "SecureString"
  value = local.deploy_langfuse ? random_password.langfuse_nextauth[0].result : "CHANGEME"
  lifecycle {
    ignore_changes = [value]
  }
}

resource "aws_ssm_parameter" "langfuse_salt" {
  name  = "/clhear/LANGFUSE_SALT"
  type  = "SecureString"
  value = local.deploy_langfuse ? random_password.langfuse_salt[0].result : "CHANGEME"
  lifecycle {
    ignore_changes = [value]
  }
}

# Project API keys the evals harness posts with (app/clhear/platform/langfuse.py);
# created in the Langfuse UI for the "CLHEAR" project and pasted here.
resource "aws_ssm_parameter" "langfuse_public_key" {
  name  = "/clhear/LANGFUSE_PUBLIC_KEY"
  type  = "SecureString"
  value = "CHANGEME"
  lifecycle {
    ignore_changes = [value]
  }
}

resource "aws_ssm_parameter" "langfuse_secret_key" {
  name  = "/clhear/LANGFUSE_SECRET_KEY"
  type  = "SecureString"
  value = "CHANGEME"
  lifecycle {
    ignore_changes = [value]
  }
}

resource "aws_security_group" "langfuse" {
  count       = local.deploy_langfuse ? 1 : 0
  name        = "${var.name_prefix}-langfuse"
  vpc_id      = var.existing_vpc_id
  description = "Langfuse web from the ALB only"

  ingress {
    from_port   = 3000
    to_port     = 3000
    protocol    = "tcp"
    cidr_blocks = var.existing_alb_cidr_blocks
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group_rule" "aurora_from_langfuse" {
  count                    = local.deploy_langfuse ? 1 : 0
  type                     = "ingress"
  from_port                = 5432
  to_port                  = 5432
  protocol                 = "tcp"
  security_group_id        = aws_security_group.aurora[0].id
  source_security_group_id = aws_security_group.langfuse[0].id
  description              = "Postgres from Langfuse"
}

resource "aws_cloudwatch_log_group" "langfuse" {
  count             = local.deploy_langfuse ? 1 : 0
  name              = "/ecs/${var.name_prefix}-langfuse"
  retention_in_days = 30
}

resource "aws_ecs_task_definition" "langfuse" {
  count                    = local.deploy_langfuse ? 1 : 0
  family                   = "${var.name_prefix}-langfuse"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 1024
  memory                   = 2048
  execution_role_arn       = aws_iam_role.worker_execution.arn

  container_definitions = jsonencode([{
    name      = "langfuse"
    image     = var.langfuse_image
    essential = true
    portMappings = [{ containerPort = 3000, protocol = "tcp" }]
    environment = [
      { name = "NEXTAUTH_URL", value = local.langfuse_url },
      { name = "HOSTNAME", value = "0.0.0.0" },
      { name = "PORT", value = "3000" },
      { name = "TELEMETRY_ENABLED", value = "false" },
      { name = "LANGFUSE_ENABLE_EXPERIMENTAL_FEATURES", value = "false" },
      { name = "AUTH_DISABLE_SIGNUP", value = "true" }, # maintainers are invited by the admin, never self-registered
      { name = "AUTH_DISABLE_USERNAME_PASSWORD", value = "false" },
      { name = "DATABASE_HOST", value = aws_rds_cluster.clhear[0].endpoint },
      { name = "DATABASE_NAME", value = "langfuse" },
      { name = "DATABASE_USERNAME", value = "langfuse" },
      { name = "DATABASE_ARGS", value = "sslmode=require" },
    ]
    secrets = [
      { name = "DATABASE_PASSWORD", valueFrom = aws_ssm_parameter.langfuse_db_password.arn },
      { name = "NEXTAUTH_SECRET", valueFrom = aws_ssm_parameter.langfuse_nextauth_secret.arn },
      { name = "SALT", valueFrom = aws_ssm_parameter.langfuse_salt.arn },
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.langfuse[0].name
        awslogs-region        = var.aws_region
        awslogs-stream-prefix = "langfuse"
      }
    }
  }])
}

resource "aws_lb_target_group" "langfuse" {
  count       = local.deploy_langfuse && var.existing_alb_listener_arn != "" ? 1 : 0
  name        = "${var.name_prefix}-langfuse"
  port        = 3000
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = var.existing_vpc_id

  health_check {
    path                = "/api/public/health"
    matcher             = "200"
    interval            = 30
    healthy_threshold   = 2
    unhealthy_threshold = 5
  }
  deregistration_delay = 30
}

resource "aws_lb_listener_rule" "langfuse_host" {
  count        = local.deploy_langfuse && var.existing_alb_listener_arn != "" ? 1 : 0
  listener_arn = var.existing_alb_listener_arn
  priority     = 45

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.langfuse[0].arn
  }
  condition {
    host_header {
      values = [var.langfuse_hostname]
    }
  }
}

resource "aws_ecs_service" "langfuse" {
  count                             = local.deploy_langfuse ? 1 : 0
  name                              = "${var.name_prefix}-langfuse"
  cluster                           = local.cluster_arn
  task_definition                   = aws_ecs_task_definition.langfuse[0].arn
  desired_count                     = 1
  launch_type                       = "FARGATE"
  enable_execute_command            = true
  health_check_grace_period_seconds = 180

  network_configuration {
    subnets          = var.existing_private_subnet_ids
    security_groups  = [aws_security_group.langfuse[0].id]
    assign_public_ip = false
  }

  dynamic "load_balancer" {
    for_each = var.existing_alb_listener_arn != "" ? [1] : []
    content {
      target_group_arn = aws_lb_target_group.langfuse[0].arn
      container_name   = "langfuse"
      container_port   = 3000
    }
  }
}

output "langfuse_url" {
  value = local.langfuse_url
}
