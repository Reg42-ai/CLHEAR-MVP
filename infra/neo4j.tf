# The query graph (HLD v2 I7, §3): Neo4j Community on Fargate with EFS-backed
# storage, reachable only from the layer fleets over Bolt. It is a projection —
# `platform/graph.py` rebuilds it from Postgres nightly (and on demand via the
# GraphRebuildRequested envelope), so losing the volume costs one rebuild, not
# the record. Enabled with neo4j_enabled = true once the VPC is known.
locals {
  deploy_neo4j   = var.neo4j_enabled && local.have_network
  neo4j_dns_name = "neo4j.${var.name_prefix}.local"
  neo4j_uri      = local.deploy_neo4j ? "bolt://${local.neo4j_dns_name}:7687" : ""
}

resource "random_password" "neo4j" {
  count   = local.deploy_neo4j ? 1 : 0
  length  = 32
  special = false
}

# The fleets read the password as a secret; the operator may rotate it in SSM
# and restart the service (the container reads NEO4J_AUTH at first boot only).
resource "aws_ssm_parameter" "neo4j_password" {
  name        = "/clhear/NEO4J_PASSWORD"
  type        = "SecureString"
  value       = local.deploy_neo4j ? random_password.neo4j[0].result : "CHANGEME"
  description = "Neo4j Community password for the CLHEAR query-graph projection"
  lifecycle {
    ignore_changes = [value]
  }
}

resource "aws_security_group" "neo4j" {
  count       = local.deploy_neo4j ? 1 : 0
  name        = "${var.name_prefix}-neo4j"
  vpc_id      = var.existing_vpc_id
  description = "CLHEAR query graph — Bolt from the layer fleets only"

  ingress {
    description     = "Bolt from fleets"
    from_port       = 7687
    to_port         = 7687
    protocol        = "tcp"
    security_groups = aws_security_group.workers[*].id
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "neo4j_efs" {
  count       = local.deploy_neo4j ? 1 : 0
  name        = "${var.name_prefix}-neo4j-efs"
  vpc_id      = var.existing_vpc_id
  description = "NFS from the Neo4j task only"

  ingress {
    from_port       = 2049
    to_port         = 2049
    protocol        = "tcp"
    security_groups = [aws_security_group.neo4j[0].id]
  }
}

resource "aws_efs_file_system" "neo4j" {
  count            = local.deploy_neo4j ? 1 : 0
  creation_token   = "${var.name_prefix}-neo4j"
  encrypted        = true
  throughput_mode  = "elastic"
  performance_mode = "generalPurpose"

  lifecycle_policy {
    transition_to_ia = "AFTER_30_DAYS"
  }

  tags = { Name = "${var.name_prefix}-neo4j" }
}

resource "aws_efs_mount_target" "neo4j" {
  count           = local.deploy_neo4j ? length(var.existing_private_subnet_ids) : 0
  file_system_id  = aws_efs_file_system.neo4j[0].id
  subnet_id       = var.existing_private_subnet_ids[count.index]
  security_groups = [aws_security_group.neo4j_efs[0].id]
}

resource "aws_efs_access_point" "neo4j_data" {
  count          = local.deploy_neo4j ? 1 : 0
  file_system_id = aws_efs_file_system.neo4j[0].id

  posix_user {
    uid = 7474
    gid = 7474
  }

  root_directory {
    path = "/data"
    creation_info {
      owner_uid   = 7474
      owner_gid   = 7474
      permissions = "0755"
    }
  }
}

resource "aws_cloudwatch_log_group" "neo4j" {
  count             = local.deploy_neo4j ? 1 : 0
  name              = "/ecs/${var.name_prefix}-neo4j"
  retention_in_days = 30
}

# Private DNS so the fleets reach the graph by a stable name (Bolt has no ALB).
resource "aws_service_discovery_private_dns_namespace" "clhear" {
  count = local.deploy_cloud_map ? 1 : 0
  name  = "${var.name_prefix}.local"
  vpc   = var.existing_vpc_id
}

resource "aws_service_discovery_service" "neo4j" {
  count = local.deploy_neo4j ? 1 : 0
  name  = "neo4j"

  dns_config {
    namespace_id   = aws_service_discovery_private_dns_namespace.clhear[0].id
    routing_policy = "MULTIVALUE"
    dns_records {
      ttl  = 10
      type = "A"
    }
  }

  health_check_custom_config {
    failure_threshold = 1
  }
}

resource "aws_ecs_task_definition" "neo4j" {
  count                    = local.deploy_neo4j ? 1 : 0
  family                   = "${var.name_prefix}-neo4j"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.neo4j_cpu
  memory                   = var.neo4j_memory
  execution_role_arn       = aws_iam_role.worker_execution.arn

  volume {
    name = "neo4j-data"
    efs_volume_configuration {
      file_system_id     = aws_efs_file_system.neo4j[0].id
      transit_encryption = "ENABLED"
      authorization_config {
        access_point_id = aws_efs_access_point.neo4j_data[0].id
        iam             = "DISABLED"
      }
    }
  }

  container_definitions = jsonencode([
    {
      name      = "neo4j"
      image     = var.neo4j_image
      essential = true
      portMappings = [
        { containerPort = 7687, protocol = "tcp" }
      ]
      environment = [
        { name = "NEO4J_server_memory_heap_initial__size", value = "${floor(var.neo4j_memory / 4)}m" },
        { name = "NEO4J_server_memory_heap_max__size", value = "${floor(var.neo4j_memory / 4)}m" },
        { name = "NEO4J_server_memory_pagecache_size", value = "${floor(var.neo4j_memory / 4)}m" },
        { name = "NEO4J_server_bolt_listen__address", value = "0.0.0.0:7687" },
        { name = "NEO4J_server_http_enabled", value = "false" },
        { name = "NEO4J_db_transaction_timeout", value = "30s" },
        { name = "NEO4J_ACCEPT_LICENSE_AGREEMENT", value = "yes" },
      ]
      secrets = [
        # NEO4J_AUTH is "user/password"; the password parameter is shared with the fleets.
        { name = "NEO4J_AUTH_PASSWORD", valueFrom = aws_ssm_parameter.neo4j_password.arn },
      ]
      # The official image reads NEO4J_AUTH; compose it from the secret at start.
      entryPoint = ["/bin/bash", "-c"]
      command    = ["export NEO4J_AUTH=\"neo4j/$${NEO4J_AUTH_PASSWORD}\"; exec /startup/docker-entrypoint.sh neo4j"]
      mountPoints = [
        { sourceVolume = "neo4j-data", containerPath = "/data", readOnly = false }
      ]
      healthCheck = {
        command     = ["CMD-SHELL", "bash -c '</dev/tcp/127.0.0.1/7687' || exit 1"]
        interval    = 30
        timeout     = 5
        retries     = 3
        startPeriod = 90
      }
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.neo4j[0].name
          awslogs-region        = var.aws_region
          awslogs-stream-prefix = "neo4j"
        }
      }
    }
  ])
}

resource "aws_ecs_service" "neo4j" {
  count                              = local.deploy_neo4j ? 1 : 0
  name                               = "${var.name_prefix}-neo4j"
  cluster                            = local.cluster_arn
  task_definition                    = aws_ecs_task_definition.neo4j[0].arn
  desired_count                      = 1
  platform_version                   = "1.4.0" # EFS volumes need >= 1.4.0
  deployment_minimum_healthy_percent = 0       # single writer: replace, never run two against one volume
  deployment_maximum_percent         = 100
  enable_execute_command             = true

  capacity_provider_strategy {
    capacity_provider = "FARGATE" # on-demand: the graph should not vanish on a Spot reclaim mid-rebuild
    weight            = 1
  }

  network_configuration {
    subnets          = var.existing_private_subnet_ids
    security_groups  = [aws_security_group.neo4j[0].id]
    assign_public_ip = false
  }

  service_registries {
    registry_arn = aws_service_discovery_service.neo4j[0].arn
  }

  depends_on = [aws_efs_mount_target.neo4j]
}

output "neo4j_uri" {
  value = local.neo4j_uri
}
