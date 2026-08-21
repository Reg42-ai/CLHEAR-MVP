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

resource "aws_ecs_task_definition" "workers" {
  count                    = var.worker_image != "" ? 1 : 0
  family                   = "${var.name_prefix}-workers"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 512
  memory                   = 1024
  execution_role_arn       = aws_iam_role.worker_execution.arn
  task_role_arn            = aws_iam_role.worker_task.arn
  container_definitions = jsonencode([
    {
      name       = "worker"
      image      = var.worker_image
      essential  = true
      entryPoint = ["python", "-m", "app.clhear.workers"]
      environment = [
        { name = "AWS_REGION", value = var.aws_region },
        { name = "CLHEAR_EVENTS_QUEUE_URL", value = aws_sqs_queue.events.url },
        { name = "CLHEAR_DATALAKE_BUCKET", value = aws_s3_bucket.datalake.bucket },
        { name = "REG42_CLHEAR_ENABLED", value = "true" },
      ]
      secrets = [
        { name = "DATABASE_URL", valueFrom = aws_ssm_parameter.database_url.arn },
        { name = "ANTHROPIC_API_KEY", valueFrom = aws_ssm_parameter.anthropic_api_key.arn },
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
  ])
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
  desired_count   = 0

  capacity_provider_strategy {
    capacity_provider = "FARGATE_SPOT"
    weight            = 1
  }

  network_configuration {
    subnets         = var.existing_private_subnet_ids
    security_groups = [aws_security_group.workers[0].id]
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
  min_capacity       = 0
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
