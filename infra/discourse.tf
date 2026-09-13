# The community forum (HLD v2 §6): Discourse on Fargate behind the existing ALB
# at discourse_hostname, with Redis as a sidecar, uploads on EFS and its
# database on the CLHEAR Aurora cluster (database `discourse`, own role). The
# forum is a conversation surface, not a layer: nothing in it is part of the
# record, and contributions still flow through the contribution API.
# Enabled with discourse_enabled = true once the VPC, Aurora and the ALB are known.
locals {
  deploy_discourse = var.discourse_enabled && local.have_network && local.deploy_aurora
  discourse_url    = local.deploy_discourse ? "https://${var.discourse_hostname}" : ""
}

resource "random_password" "discourse_db" {
  count   = local.deploy_discourse ? 1 : 0
  length  = 32
  special = false
}

resource "random_password" "discourse_admin" {
  count   = local.deploy_discourse ? 1 : 0
  length  = 24
  special = false
}

# Created by the operator once (psql on the Aurora cluster):
#   CREATE ROLE discourse LOGIN PASSWORD '<value of /clhear/DISCOURSE_DB_PASSWORD>';
#   CREATE DATABASE discourse OWNER discourse;
#   \c discourse  CREATE EXTENSION IF NOT EXISTS hstore; CREATE EXTENSION IF NOT EXISTS pg_trgm;
resource "aws_ssm_parameter" "discourse_db_password" {
  name        = "/clhear/DISCOURSE_DB_PASSWORD"
  type        = "SecureString"
  value       = local.deploy_discourse ? random_password.discourse_db[0].result : "CHANGEME"
  description = "Postgres password of the `discourse` role on the CLHEAR Aurora cluster"
  lifecycle {
    ignore_changes = [value]
  }
}

resource "aws_ssm_parameter" "discourse_admin_password" {
  name        = "/clhear/DISCOURSE_ADMIN_PASSWORD"
  type        = "SecureString"
  value       = local.deploy_discourse ? random_password.discourse_admin[0].result : "CHANGEME"
  description = "Initial Discourse admin password (rotate in the forum after first login)"
  lifecycle {
    ignore_changes = [value]
  }
}

# SMTP credentials for notification mail: SES SMTP user, filled in by the operator.
resource "aws_ssm_parameter" "discourse_smtp_user" {
  name  = "/clhear/DISCOURSE_SMTP_USER"
  type  = "SecureString"
  value = "CHANGEME"
  lifecycle {
    ignore_changes = [value]
  }
}

resource "aws_ssm_parameter" "discourse_smtp_password" {
  name  = "/clhear/DISCOURSE_SMTP_PASSWORD"
  type  = "SecureString"
  value = "CHANGEME"
  lifecycle {
    ignore_changes = [value]
  }
}

resource "aws_security_group" "discourse" {
  count       = local.deploy_discourse ? 1 : 0
  name        = "${var.name_prefix}-discourse"
  vpc_id      = var.existing_vpc_id
  description = "CLHEAR forum — HTTP from the ALB only"

  ingress {
    description = "HTTP from the ALB"
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

# The forum reaches Aurora on the same port the fleets use.
resource "aws_security_group_rule" "aurora_from_discourse" {
  count                    = local.deploy_discourse ? 1 : 0
  type                     = "ingress"
  description              = "Postgres from the forum"
  from_port                = 5432
  to_port                  = 5432
  protocol                 = "tcp"
  security_group_id        = aws_security_group.aurora[0].id
  source_security_group_id = aws_security_group.discourse[0].id
}

resource "aws_security_group" "discourse_efs" {
  count       = local.deploy_discourse ? 1 : 0
  name        = "${var.name_prefix}-discourse-efs"
  vpc_id      = var.existing_vpc_id
  description = "NFS from the forum task only"

  ingress {
    from_port       = 2049
    to_port         = 2049
    protocol        = "tcp"
    security_groups = [aws_security_group.discourse[0].id]
  }
}

resource "aws_efs_file_system" "discourse" {
  count            = local.deploy_discourse ? 1 : 0
  creation_token   = "${var.name_prefix}-discourse"
  encrypted        = true
  throughput_mode  = "elastic"
  performance_mode = "generalPurpose"

  lifecycle_policy {
    transition_to_ia = "AFTER_30_DAYS"
  }

  tags = { Name = "${var.name_prefix}-discourse" }
}

resource "aws_efs_mount_target" "discourse" {
  count           = local.deploy_discourse ? length(var.existing_private_subnet_ids) : 0
  file_system_id  = aws_efs_file_system.discourse[0].id
  subnet_id       = var.existing_private_subnet_ids[count.index]
  security_groups = [aws_security_group.discourse_efs[0].id]
}

resource "aws_efs_access_point" "discourse_uploads" {
  count          = local.deploy_discourse ? 1 : 0
  file_system_id = aws_efs_file_system.discourse[0].id

  posix_user {
    uid = 1001
    gid = 1001
  }

  root_directory {
    path = "/uploads"
    creation_info {
      owner_uid   = 1001
      owner_gid   = 1001
      permissions = "0755"
    }
  }
}

resource "aws_cloudwatch_log_group" "discourse" {
  count             = local.deploy_discourse ? 1 : 0
  name              = "/ecs/${var.name_prefix}-discourse"
  retention_in_days = 30
}

locals {
  discourse_env = [
    { name = "DISCOURSE_HOST", value = var.discourse_hostname },
    { name = "DISCOURSE_SITE_NAME", value = "CLHEAR community" },
    { name = "DISCOURSE_USERNAME", value = "clhear-admin" },
    { name = "DISCOURSE_EMAIL", value = "community@${var.clhear_hostname}" },
    { name = "DISCOURSE_ENABLE_HTTPS", value = "yes" },
    { name = "DISCOURSE_DATABASE_HOST", value = local.deploy_discourse ? aws_rds_cluster.clhear[0].endpoint : "" },
    { name = "DISCOURSE_DATABASE_PORT_NUMBER", value = "5432" },
    { name = "DISCOURSE_DATABASE_NAME", value = "discourse" },
    { name = "DISCOURSE_DATABASE_USER", value = "discourse" },
    { name = "DISCOURSE_REDIS_HOST", value = "127.0.0.1" },
    { name = "DISCOURSE_REDIS_PORT_NUMBER", value = "6379" },
    { name = "DISCOURSE_SMTP_HOST", value = "email-smtp.${var.aws_region}.amazonaws.com" },
    { name = "DISCOURSE_SMTP_PORT", value = "587" },
    { name = "DISCOURSE_SMTP_PROTOCOL", value = "tls" },
    { name = "DISCOURSE_SMTP_AUTH", value = "login" },
  ]
  discourse_secrets = [
    { name = "DISCOURSE_DATABASE_PASSWORD", valueFrom = aws_ssm_parameter.discourse_db_password.arn },
    { name = "DISCOURSE_PASSWORD", valueFrom = aws_ssm_parameter.discourse_admin_password.arn },
    { name = "DISCOURSE_SMTP_USER", valueFrom = aws_ssm_parameter.discourse_smtp_user.arn },
    { name = "DISCOURSE_SMTP_PASSWORD", valueFrom = aws_ssm_parameter.discourse_smtp_password.arn },
  ]
}

resource "aws_ecs_task_definition" "discourse" {
  count                    = local.deploy_discourse ? 1 : 0
  family                   = "${var.name_prefix}-discourse"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.discourse_cpu
  memory                   = var.discourse_memory
  execution_role_arn       = aws_iam_role.worker_execution.arn

  volume {
    name = "discourse-uploads"
    efs_volume_configuration {
      file_system_id     = aws_efs_file_system.discourse[0].id
      transit_encryption = "ENABLED"
      authorization_config {
        access_point_id = aws_efs_access_point.discourse_uploads[0].id
        iam             = "DISABLED"
      }
    }
  }

  container_definitions = jsonencode([
    {
      name      = "redis"
      image     = var.discourse_redis_image
      essential = true
      portMappings = [
        { containerPort = 6379, protocol = "tcp" }
      ]
      environment = [
        { name = "ALLOW_EMPTY_PASSWORD", value = "yes" },
      ]
      healthCheck = {
        command     = ["CMD-SHELL", "redis-cli ping | grep -q PONG"]
        interval    = 15
        timeout     = 5
        retries     = 3
        startPeriod = 10
      }
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.discourse[0].name
          awslogs-region        = var.aws_region
          awslogs-stream-prefix = "redis"
        }
      }
    },
    {
      name      = "discourse"
      image     = var.discourse_image
      essential = true
      portMappings = [
        { containerPort = 3000, protocol = "tcp" }
      ]
      environment = local.discourse_env
      secrets     = local.discourse_secrets
      dependsOn = [
        { containerName = "redis", condition = "HEALTHY" }
      ]
      mountPoints = [
        { sourceVolume = "discourse-uploads", containerPath = "/bitnami/discourse/public/uploads", readOnly = false }
      ]
      healthCheck = {
        command     = ["CMD-SHELL", "curl -fsS http://127.0.0.1:3000/srv/status || exit 1"]
        interval    = 30
        timeout     = 5
        retries     = 5
        startPeriod = 300 # first boot runs migrations and precompiles assets
      }
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.discourse[0].name
          awslogs-region        = var.aws_region
          awslogs-stream-prefix = "discourse"
        }
      }
    },
    {
      name        = "sidekiq"
      image       = var.discourse_image
      essential   = false
      command     = ["/opt/bitnami/scripts/discourse-sidekiq/run.sh"]
      environment = local.discourse_env
      secrets     = local.discourse_secrets
      dependsOn = [
        { containerName = "discourse", condition = "HEALTHY" }
      ]
      mountPoints = [
        { sourceVolume = "discourse-uploads", containerPath = "/bitnami/discourse/public/uploads", readOnly = false }
      ]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.discourse[0].name
          awslogs-region        = var.aws_region
          awslogs-stream-prefix = "sidekiq"
        }
      }
    }
  ])
}

resource "aws_lb_target_group" "discourse" {
  count       = local.deploy_discourse && var.existing_alb_listener_arn != "" ? 1 : 0
  name        = "${var.name_prefix}-discourse"
  port        = 3000
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = var.existing_vpc_id

  health_check {
    path                = "/srv/status"
    matcher             = "200"
    interval            = 30
    healthy_threshold   = 2
    unhealthy_threshold = 5
  }

  deregistration_delay = 30
}

# Host rule discourse_hostname -> the forum, on the same listener as clhear.reg42.ai.
resource "aws_lb_listener_rule" "discourse_host" {
  count        = local.deploy_discourse && var.existing_alb_listener_arn != "" ? 1 : 0
  listener_arn = var.existing_alb_listener_arn
  priority     = 43

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.discourse[0].arn
  }

  condition {
    host_header {
      values = [var.discourse_hostname]
    }
  }
}

resource "aws_ecs_service" "discourse" {
  count                              = local.deploy_discourse ? 1 : 0
  name                               = "${var.name_prefix}-discourse"
  cluster                            = local.cluster_arn
  task_definition                    = aws_ecs_task_definition.discourse[0].arn
  desired_count                      = 1
  platform_version                   = "1.4.0" # EFS volumes need >= 1.4.0
  deployment_minimum_healthy_percent = 0       # one Sidekiq + one Redis: replace, never run two against one upload volume
  deployment_maximum_percent         = 100
  enable_execute_command             = true
  health_check_grace_period_seconds  = 600

  capacity_provider_strategy {
    capacity_provider = "FARGATE" # on-demand: a community surface should not vanish on a Spot reclaim
    weight            = 1
  }

  network_configuration {
    subnets          = var.existing_private_subnet_ids
    security_groups  = [aws_security_group.discourse[0].id]
    assign_public_ip = false
  }

  dynamic "load_balancer" {
    for_each = var.existing_alb_listener_arn != "" ? [1] : []
    content {
      target_group_arn = aws_lb_target_group.discourse[0].arn
      container_name   = "discourse"
      container_port   = 3000
    }
  }

  depends_on = [aws_efs_mount_target.discourse]
}

# The web app links to the forum from /contribute when this is set.
resource "aws_ssm_parameter" "discourse_url" {
  name  = "/clhear/CLHEAR_DISCOURSE_URL"
  type  = "String"
  value = local.discourse_url != "" ? local.discourse_url : "unset"
  lifecycle {
    ignore_changes = [value]
  }
}

output "discourse_url" {
  value = local.discourse_url
}
