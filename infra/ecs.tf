# Per-layer fleets (HLD v2 §3): one Fargate Spot task definition + service per
# layer, each consuming its own SQS queue, min 0 / scale on queue depth.
# Inference is remote (Reg42 Infer on Bedrock, I6) — no model runtime here.
locals {
  create_cluster = var.existing_ecs_cluster_arn == ""
  cluster_arn    = local.create_cluster ? aws_ecs_cluster.clhear[0].arn : var.existing_ecs_cluster_arn
  have_network   = var.existing_vpc_id != "" && length(var.existing_private_subnet_ids) > 0
  deploy_workers = var.worker_image != "" && local.have_network

  # layer -> fleet sizing. L1 ingest is I/O heavy (dozens of sources per night);
  # derivation fleets are thin clients of Infer.
  fleets = {
    l0 = { cpu = 512, memory = 1024, max = 1, description = "platform: relay, releases, gates, approvals" }
    l1 = { cpu = 1024, memory = 2048, max = var.worker_max_count, description = "verbatim corpus adapters" }
    l2 = { cpu = 512, memory = 1024, max = 2, description = "obligations: extract / consolidate / change / judge" }
    l3 = { cpu = 512, memory = 1024, max = 2, description = "building blocks: decompose / characterize / harmonize" }
    l4 = { cpu = 512, memory = 1024, max = 1, description = "profiles: registers / predicates / builder" }
    l5 = { cpu = 512, memory = 1024, max = 1, description = "activities: sides / implies / mitigates" }
    l6 = { cpu = 512, memory = 1024, max = 1, description = "composer: blueprints / minimality / explain" }
    l7 = { cpu = 512, memory = 1024, max = 1, description = "risk: enforcement ingestion / linking / scoring" }
    l8 = { cpu = 512, memory = 1024, max = 1, description = "fills: maturity / member aggregates" }
  }
  # L1 keeps the original queue (schedules and the web app already target it).
  fleet_queue = merge({ l1 = aws_sqs_queue.events }, aws_sqs_queue.fleet)
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

resource "aws_cloudwatch_log_group" "fleet" {
  for_each          = local.fleets
  name              = "/ecs/${var.name_prefix}-fleet-${each.key}"
  retention_in_days = 30
}

resource "aws_ecs_task_definition" "fleet" {
  for_each                 = var.worker_image != "" ? local.fleets : {}
  family                   = "${var.name_prefix}-fleet-${each.key}"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = each.value.cpu
  memory                   = each.value.memory
  execution_role_arn       = aws_iam_role.worker_execution.arn
  task_role_arn            = aws_iam_role.worker_task.arn

  container_definitions = jsonencode([
    {
      name       = "worker"
      image      = var.worker_image
      essential  = true
      cpu        = each.value.cpu
      memory     = each.value.memory
      entryPoint = ["python", "-m", "app.clhear.workers"]
      environment = [
        { name = "AWS_REGION", value = var.aws_region },
        { name = "CLHEAR_FLEET", value = upper(each.key) },
        { name = "CLHEAR_EVENTS_QUEUE_URL", value = local.fleet_queue[each.key].url },
        { name = "CLHEAR_EVENTS_DLQ_URL", value = aws_sqs_queue.events_dlq.url },
        { name = "CLHEAR_EVENT_BUS_NAME", value = aws_cloudwatch_event_bus.clhear.name },
        { name = "CLHEAR_DATALAKE_BUCKET", value = aws_s3_bucket.datalake.bucket },
        { name = "REG42_CLHEAR_ENABLED", value = "true" },
        { name = "CLHEAR_SNAPSHOT_S3_URI", value = var.aurora_enabled ? "" : "s3://${aws_s3_bucket.deploy.bucket}/webui/clhear-latest.db" },
        { name = "CLHEAR_RELEASES_S3_PREFIX", value = "s3://${aws_s3_bucket.deploy.bucket}/releases" },
        { name = "CLHEAR_HTTP_MODE", value = "live" },
        { name = "CLHEAR_ARTIFACT_STORE", value = "s3" },
        { name = "CLHEAR_LLM_PROVIDER", value = "infer" },
        { name = "INFER_BASE_URL", value = var.infer_base_url },
        { name = "INFER_EMPLOYEE_ID", value = "clhear-${each.key}" },
        { name = "CLHEAR_FRONTIER_MONTHLY_CAP_USD", value = "50" },
        # Query graph: empty URI -> in-process projection over Postgres (same answers, no Bolt).
        { name = "CLHEAR_NEO4J_URI", value = local.neo4j_uri },
        { name = "CLHEAR_NEO4J_USER", value = "neo4j" },
        { name = "CLHEAR_EMBEDDING_PROVIDER", value = "infer" },
      ]
      secrets = [
        { name = "DATABASE_URL", valueFrom = aws_ssm_parameter.database_url.arn },
        { name = "INFER_TOKEN", valueFrom = aws_ssm_parameter.infer_token.arn },
        { name = "CLHEAR_NEO4J_PASSWORD", valueFrom = aws_ssm_parameter.neo4j_password.arn },
      ]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.fleet[each.key].name
          awslogs-region        = var.aws_region
          awslogs-stream-prefix = each.key
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

resource "aws_ecs_service" "fleet" {
  for_each        = local.deploy_workers ? local.fleets : {}
  name            = "${var.name_prefix}-fleet-${each.key}"
  cluster         = local.cluster_arn
  task_definition = aws_ecs_task_definition.fleet[each.key].arn
  desired_count   = 0 # near-zero idle: autoscaling raises it when the fleet queue has work

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

resource "aws_appautoscaling_target" "fleet" {
  for_each           = local.deploy_workers ? local.fleets : {}
  service_namespace  = "ecs"
  resource_id        = "service/${split("/", local.cluster_arn)[1]}/${aws_ecs_service.fleet[each.key].name}"
  scalable_dimension = "ecs:service:DesiredCount"
  min_capacity       = 0
  max_capacity       = each.value.max
}

resource "aws_appautoscaling_policy" "fleet_scale_out" {
  for_each           = local.deploy_workers ? local.fleets : {}
  name               = "${var.name_prefix}-fleet-${each.key}-scale-out"
  service_namespace  = "ecs"
  resource_id        = aws_appautoscaling_target.fleet[each.key].resource_id
  scalable_dimension = aws_appautoscaling_target.fleet[each.key].scalable_dimension
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

resource "aws_appautoscaling_policy" "fleet_scale_in" {
  for_each           = local.deploy_workers ? local.fleets : {}
  name               = "${var.name_prefix}-fleet-${each.key}-scale-in"
  service_namespace  = "ecs"
  resource_id        = aws_appautoscaling_target.fleet[each.key].resource_id
  scalable_dimension = aws_appautoscaling_target.fleet[each.key].scalable_dimension
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

resource "aws_cloudwatch_metric_alarm" "fleet_queue_has_messages" {
  for_each            = local.deploy_workers ? local.fleets : {}
  alarm_name          = "${var.name_prefix}-fleet-${each.key}-queue-has-messages"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = local.fleet_queue[each.key].name }
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  alarm_actions       = [aws_appautoscaling_policy.fleet_scale_out[each.key].arn]
  ok_actions          = [aws_appautoscaling_policy.fleet_scale_in[each.key].arn]
}
