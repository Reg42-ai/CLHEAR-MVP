# clhear-workers: Fargate Spot, same image as reg42-os, min 0 / max 2,
# scale on SQS depth (HLD §5 — near-zero idle).
locals {
  create_cluster = var.existing_ecs_cluster_arn == ""
  cluster_arn    = local.create_cluster ? aws_ecs_cluster.clhear[0].arn : var.existing_ecs_cluster_arn
  have_network   = var.existing_vpc_id != "" && length(var.existing_private_subnet_ids) > 0
  deploy_workers = var.worker_image != "" && local.have_network
}

resource "aws_ecs_cluster" "clhear" {
  count = local.create_cluster ? 1 : 0
  name  = "${var.name_prefix}-cluster"
}

resource "aws_ecs_cluster_capacity_providers" "clhear" {
  count              = local.create_cluster ? 1 : 0
  cluster_name       = aws_ecs_cluster.clhear[0].name
  capacity_providers = ["FARGATE_SPOT", "FARGATE"]
}

resource "aws_cloudwatch_log_group" "workers" {
  name              = "/ecs/${var.name_prefix}-workers"
  retention_in_days = 30
}

locals {
  worker_cpu    = var.ollama_sidecar_enabled ? 4096 : 512
  worker_memory = var.ollama_sidecar_enabled ? 16384 : 1024
  # k8s request/limit → Fargate: container cpu + memoryReservation (request),
  # cpu + memory (hard limit). Sums must fit the task 4 vCPU / 16 GB.
  worker_container_cpu     = var.ollama_sidecar_enabled ? 1024 : 512
  worker_container_memory  = var.ollama_sidecar_enabled ? 2048 : 1024
  ollama_container_cpu     = 3072
  ollama_container_memory  = 14336
  worker_container = {
    name              = "worker"
    image             = var.worker_image
    essential         = true
    cpu               = local.worker_container_cpu
    memory            = local.worker_container_memory
    memoryReservation = var.ollama_sidecar_enabled ? 512 : 256
    entryPoint        = ["python", "-m", "app.clhear.workers"]
    environment = concat(
      [
        { name = "AWS_REGION", value = var.aws_region },
        { name = "CLHEAR_EVENTS_QUEUE_URL", value = aws_sqs_queue.events.url },
        { name = "CLHEAR_DATALAKE_BUCKET", value = aws_s3_bucket.datalake.bucket },
        { name = "REG42_CLHEAR_ENABLED", value = "true" },
        { name = "CLHEAR_SNAPSHOT_S3_URI", value = "s3://${aws_s3_bucket.deploy.bucket}/webui/clhear-latest.db" },
        { name = "CLHEAR_RELEASES_S3_PREFIX", value = "s3://${aws_s3_bucket.deploy.bucket}/releases" },
        { name = "CLHEAR_HTTP_MODE", value = "live" },
        { name = "CLHEAR_ARTIFACT_STORE", value = "s3" },
        { name = "CLHEAR_FRONTIER_MONTHLY_CAP_USD", value = "50" },
        { name = "CLHEAR_OLLAMA_MODEL_CACHE_S3", value = "s3://${aws_s3_bucket.deploy.bucket}/ollama-models" },
        { name = "CLHEAR_GPU_INSTANCE_PROFILE", value = aws_iam_instance_profile.gpu.name },
      ],
      length(var.existing_private_subnet_ids) > 0 ? [{ name = "CLHEAR_GPU_SUBNET_ID", value = var.existing_private_subnet_ids[0] }] : [],
      local.have_network ? [{ name = "CLHEAR_GPU_SECURITY_GROUP_ID", value = aws_security_group.gpu[0].id }] : [],
      var.ollama_sidecar_enabled ? [{ name = "OLLAMA_BASE_URL", value = "http://127.0.0.1:11434" }] : [],
    )
    secrets = [
      { name = "DATABASE_URL", valueFrom = aws_ssm_parameter.database_url.arn },
      { name = "OLLAMA_API_KEY", valueFrom = aws_ssm_parameter.ollama_api_key.arn },
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.workers.name
        awslogs-region        = var.aws_region
        awslogs-stream-prefix = "worker"
      }
    }
  }
  ollama_sidecar = {
    name              = "ollama"
    image             = var.worker_image
    essential         = true
    cpu               = local.ollama_container_cpu
    memory            = local.ollama_container_memory
    memoryReservation = 12288
    entryPoint        = ["python", "-m", "app.clhear.platform.ollama_sidecar"]
    portMappings = [{ containerPort = 11434, protocol = "tcp" }]
    environment = [
      { name = "OLLAMA_HOST", value = "0.0.0.0:11434" },
      { name = "OLLAMA_KEEP_ALIVE", value = "24h" },
      { name = "OLLAMA_MAX_LOADED_MODELS", value = "1" },
      { name = "AWS_REGION", value = var.aws_region },
      { name = "CLHEAR_OLLAMA_MODEL_CACHE_S3", value = "s3://${aws_s3_bucket.deploy.bucket}/ollama-models" },
      { name = "CLHEAR_OLLAMA_CPU_MODELS", value = "qwen3.5:4b,qwen3.5:9b" },
      { name = "CLHEAR_OLLAMA_METRIC_ROLE", value = "sidecar" },
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.workers.name
        awslogs-region        = var.aws_region
        awslogs-stream-prefix = "ollama"
      }
    }
  }
}

resource "aws_ecs_task_definition" "workers" {
  count                    = var.worker_image != "" ? 1 : 0
  family                   = "${var.name_prefix}-workers"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = local.worker_cpu
  memory                   = local.worker_memory
  execution_role_arn       = aws_iam_role.worker_execution.arn
  task_role_arn            = aws_iam_role.worker_task.arn
  ephemeral_storage {
    size_in_gib = var.ollama_sidecar_enabled ? 60 : 21
  }
  # jsonencode each branch so the ternary stays string/string (object
  # tuples of different length are not a legal terraform type).
  container_definitions = var.ollama_sidecar_enabled ? jsonencode([local.worker_container, local.ollama_sidecar]) : jsonencode([local.worker_container])
}

resource "aws_security_group" "workers" {
  count  = local.have_network ? 1 : 0
  name   = "${var.name_prefix}-workers"
  vpc_id = var.existing_vpc_id
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_ecs_service" "workers" {
  count           = local.deploy_workers ? 1 : 0
  name            = "${var.name_prefix}-workers"
  cluster         = local.cluster_arn
  task_definition = aws_ecs_task_definition.workers[0].arn
  desired_count   = 1

  capacity_provider_strategy {
    capacity_provider = "FARGATE_SPOT"
    weight            = 1
  }

  network_configuration {
    subnets          = var.existing_private_subnet_ids
    security_groups  = [aws_security_group.workers[0].id]
    assign_public_ip = var.worker_assign_public_ip
  }

  lifecycle {
    ignore_changes = [desired_count] # autoscaling owns it
  }
}

resource "aws_appautoscaling_target" "workers" {
  count              = local.deploy_workers ? 1 : 0
  service_namespace  = "ecs"
  resource_id        = "service/${split("/", local.cluster_arn)[1]}/${aws_ecs_service.workers[0].name}"
  scalable_dimension = "ecs:service:DesiredCount"
  min_capacity       = 1
  max_capacity       = var.worker_max_count
}

resource "aws_appautoscaling_policy" "workers_scale_out" {
  count              = local.deploy_workers ? 1 : 0
  name               = "${var.name_prefix}-workers-scale-out"
  service_namespace  = "ecs"
  resource_id        = aws_appautoscaling_target.workers[0].resource_id
  scalable_dimension = aws_appautoscaling_target.workers[0].scalable_dimension
  policy_type        = "StepScaling"
  step_scaling_policy_configuration {
    adjustment_type         = "ExactCapacity"
    metric_aggregation_type = "Maximum"
    step_adjustment {
      metric_interval_lower_bound = 0
      scaling_adjustment          = 1
    }
  }
}

resource "aws_appautoscaling_policy" "workers_scale_in" {
  count              = local.deploy_workers ? 1 : 0
  name               = "${var.name_prefix}-workers-scale-in"
  service_namespace  = "ecs"
  resource_id        = aws_appautoscaling_target.workers[0].resource_id
  scalable_dimension = aws_appautoscaling_target.workers[0].scalable_dimension
  policy_type        = "StepScaling"
  step_scaling_policy_configuration {
    adjustment_type         = "ExactCapacity"
    metric_aggregation_type = "Maximum"
    step_adjustment {
      metric_interval_upper_bound = 0
      scaling_adjustment          = 0
    }
  }
}

resource "aws_cloudwatch_metric_alarm" "queue_has_messages" {
  count               = local.deploy_workers ? 1 : 0
  alarm_name          = "${var.name_prefix}-queue-has-messages"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = aws_sqs_queue.events.name }
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  alarm_actions       = [aws_appautoscaling_policy.workers_scale_out[0].arn]
  ok_actions          = [aws_appautoscaling_policy.workers_scale_in[0].arn]
}
