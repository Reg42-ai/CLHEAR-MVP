# Observability (HLD v2 §3 "Observability", §7.1 SLOs; items 10 and 17).
#
#   Prometheus  — a Prometheus agent on Fargate scrapes /metrics (freshness, gate
#                 status, audit counters, DR drill) every minute and remote-writes to
#                 Amazon Managed Prometheus (AMP). No client library in the app: the
#                 exposition is rendered by app/clhear/platform/metrics.py.
#   Grafana     — Amazon Managed Grafana over AMP + CloudWatch, SSO via IAM Identity Center.
#   GlitchTip   — self-hosted, Sentry-protocol error tracking on Fargate (Aurora database
#                 `glitchtip`, Redis sidecar) behind the shared ALB; the web tier and the
#                 fleets report through SENTRY_DSN (app/clhear/platform/errors.py, scrubbed).
#   CloudWatch  — SLO alarms (API 5xx ratio, DR drill, freshness) into one SNS topic.
#   Status page — /status (+ /status.json, probed by Upptime: status/.upptimerc.yml).
#
# Everything here is public-facing *about* the service, never *of* the record:
# metrics carry counts and ages, no text and no identifiers.

variable "observability_enabled" {
  type        = bool
  default     = false
  description = "Deploy AMP + Prometheus agent + Managed Grafana + GlitchTip (needs the VPC and Aurora)."
}

variable "glitchtip_hostname" {
  type    = string
  default = "errors.clhear.reg42.ai"
}

variable "glitchtip_image" {
  type    = string
  default = "glitchtip/glitchtip:v4.2"
}

variable "prometheus_image" {
  type    = string
  default = "prom/prometheus:v2.54.1"
}

variable "alerts_email" {
  type        = string
  default     = "ops@reg42.ai"
  description = "Where SLO alarms are delivered (the incident-response on-call address)."
}

locals {
  deploy_observability = var.observability_enabled && local.have_network
  deploy_glitchtip     = local.deploy_observability && local.deploy_aurora
}

# --- Alerts -------------------------------------------------------------------

resource "aws_sns_topic" "alerts" {
  name = "${var.name_prefix}-alerts"
}

resource "aws_sns_topic_subscription" "alerts_email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alerts_email
}

# API availability SLO (99.9 % / 30 d) — the leading indicator: 5xx ratio on the
# public API over 5 minutes. Upptime keeps the 30-day figure independently.
resource "aws_cloudwatch_metric_alarm" "api_5xx_ratio" {
  count               = local.deploy_webui ? 1 : 0
  alarm_name          = "${var.name_prefix}-api-5xx-ratio"
  alarm_description   = "Public API 5xx ratio above 0.1 % over 5 minutes (SLO: 99.9 % availability)."
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  threshold           = 0.001
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]

  metric_query {
    id = "ratio"
    # MAX() only accepts time series, so guard the division with IF() instead.
    expression  = "IF(requests > 0, errors / requests, 0)"
    label       = "5xx ratio"
    return_data = true
  }
  metric_query {
    id = "errors"
    metric {
      namespace   = "AWS/ApiGateway"
      metric_name = "5xx"
      period      = 300
      stat        = "Sum"
      dimensions  = { ApiId = aws_apigatewayv2_api.webui[0].id }
    }
  }
  metric_query {
    id = "requests"
    metric {
      namespace   = "AWS/ApiGateway"
      metric_name = "Count"
      period      = 300
      stat        = "Sum"
      dimensions  = { ApiId = aws_apigatewayv2_api.webui[0].id }
    }
  }
}

# DR drill (item 17): the nightly restore must run and pass. No metric for two
# days = the drill did not run = breaching (a drill that never ran proves nothing).
resource "aws_cloudwatch_metric_alarm" "dr_drill_failed" {
  alarm_name          = "${var.name_prefix}-dr-drill-failed"
  alarm_description   = "The nightly restore drill (Postgres + Neo4j + datalake replica) failed or did not run in 48 h."
  namespace           = "CLHEAR"
  metric_name         = "DrDrillPassed"
  statistic           = "Minimum"
  period              = 172800
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "breaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
}

# Freshness SLO (Tier-A ≤ 24 h) as seen by the Prometheus scrape of /metrics — the
# Grafana alert rule below fires on clhear_layer_fresh{layer="L1"} == 0. The
# CloudWatch alarm on ScheduleMissedSources (cloudwatch.tf) is its upstream cause.

# --- Prometheus: AMP workspace + agent on Fargate --------------------------------

resource "aws_prometheus_workspace" "clhear" {
  count = local.deploy_observability ? 1 : 0
  alias = "${var.name_prefix}-metrics"
}

resource "aws_cloudwatch_log_group" "prometheus" {
  count             = local.deploy_observability ? 1 : 0
  name              = "/ecs/${var.name_prefix}-prometheus"
  retention_in_days = 14
}

resource "aws_iam_role" "prometheus_agent" {
  count = local.deploy_observability ? 1 : 0
  name  = "${var.name_prefix}-prometheus-agent"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "ecs-tasks.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy" "prometheus_agent_remote_write" {
  count = local.deploy_observability ? 1 : 0
  name  = "amp-remote-write"
  role  = aws_iam_role.prometheus_agent[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["aps:RemoteWrite", "aps:GetSeries", "aps:GetLabels", "aps:GetMetricMetadata"]
      Resource = aws_prometheus_workspace.clhear[0].arn
    }]
  })
}

locals {
  prometheus_config = local.deploy_observability ? yamlencode({
    global = { scrape_interval = "60s", scrape_timeout = "20s" }
    scrape_configs = [{
      job_name        = "clhear-api"
      scheme          = "https"
      metrics_path    = "/metrics"
      static_configs  = [{ targets = [var.clhear_hostname], labels = { service = "clhear", tier = "web" } }]
      scrape_interval = "60s"
    }]
    remote_write = [{
      url          = "${aws_prometheus_workspace.clhear[0].prometheus_endpoint}api/v1/remote_write"
      sigv4        = { region = var.aws_region }
      queue_config = { max_samples_per_send = 1000, max_shards = 10, capacity = 2500 }
    }]
  }) : ""
}

resource "aws_ecs_task_definition" "prometheus" {
  count                    = local.deploy_observability ? 1 : 0
  family                   = "${var.name_prefix}-prometheus"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 512
  execution_role_arn       = aws_iam_role.worker_execution.arn
  task_role_arn            = aws_iam_role.prometheus_agent[0].arn

  container_definitions = jsonencode([{
    name       = "prometheus"
    image      = var.prometheus_image
    essential  = true
    entryPoint = ["/bin/sh", "-c"]
    # Agent mode: scrape and forward, no local TSDB to keep (the workspace is the store).
    command     = ["printf '%s' \"$PROMETHEUS_CONFIG\" > /tmp/prometheus.yml && exec /bin/prometheus --config.file=/tmp/prometheus.yml --enable-feature=agent --storage.agent.path=/tmp/agent"]
    environment = [{ name = "PROMETHEUS_CONFIG", value = local.prometheus_config }]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.prometheus[0].name
        awslogs-region        = var.aws_region
        awslogs-stream-prefix = "prometheus"
      }
    }
  }])
}

resource "aws_ecs_service" "prometheus" {
  count           = local.deploy_observability ? 1 : 0
  name            = "${var.name_prefix}-prometheus"
  cluster         = local.cluster_arn
  task_definition = aws_ecs_task_definition.prometheus[0].arn
  desired_count   = 1
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = var.existing_private_subnet_ids
    security_groups  = [aws_security_group.workers[0].id]
    assign_public_ip = var.worker_assign_public_ip
  }
}

# --- Grafana (managed) over AMP + CloudWatch -------------------------------------

resource "aws_iam_role" "grafana" {
  count = local.deploy_observability ? 1 : 0
  name  = "${var.name_prefix}-grafana"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "grafana.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy" "grafana_read" {
  count = local.deploy_observability ? 1 : 0
  name  = "read-amp-cloudwatch"
  role  = aws_iam_role.grafana[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["aps:ListWorkspaces", "aps:DescribeWorkspace", "aps:QueryMetrics", "aps:GetLabels", "aps:GetSeries", "aps:GetMetricMetadata"]
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = ["cloudwatch:DescribeAlarmsForMetric", "cloudwatch:DescribeAlarmHistory", "cloudwatch:DescribeAlarms", "cloudwatch:ListMetrics", "cloudwatch:GetMetricData", "cloudwatch:GetInsightRuleReport", "logs:DescribeLogGroups", "logs:GetLogGroupFields", "logs:StartQuery", "logs:StopQuery", "logs:GetQueryResults", "logs:GetLogEvents"]
        Resource = "*"
      },
    ]
  })
}

resource "aws_grafana_workspace" "clhear" {
  count                     = local.deploy_observability ? 1 : 0
  name                      = "${var.name_prefix}-grafana"
  account_access_type       = "CURRENT_ACCOUNT"
  authentication_providers  = ["AWS_SSO"]
  permission_type           = "SERVICE_MANAGED"
  role_arn                  = aws_iam_role.grafana[0].arn
  data_sources              = ["PROMETHEUS", "CLOUDWATCH"]
  notification_destinations = ["SNS"]
}

# Grafana alert rules and the "CLHEAR SLOs" dashboard are provisioned from
# status/grafana/ by the release job (grafana HTTP API); the rules are:
#   clhear_layer_fresh{layer="L1"} == 0 for 1h        → Tier-A freshness SLO missed
#   min(clhear_gate_passed) == 0                        → a layer is below its gate
#   clhear_dr_drill_passed == 0 or clhear_dr_drill_age_seconds > 172800 → DR drill
#   absent(clhear_up) for 5m                            → scrape target down

# --- GlitchTip: self-hosted error tracking -----------------------------------------

resource "random_password" "glitchtip_db" {
  count   = local.deploy_glitchtip ? 1 : 0
  length  = 32
  special = false
}

resource "random_password" "glitchtip_secret" {
  count   = local.deploy_glitchtip ? 1 : 0
  length  = 50
  special = false
}

# Created by the operator once (psql on the Aurora cluster):
#   CREATE ROLE glitchtip LOGIN PASSWORD '<value of /clhear/GLITCHTIP_DB_PASSWORD>';
#   CREATE DATABASE glitchtip OWNER glitchtip;
resource "aws_ssm_parameter" "glitchtip_db_password" {
  name        = "/clhear/GLITCHTIP_DB_PASSWORD"
  type        = "SecureString"
  value       = local.deploy_glitchtip ? random_password.glitchtip_db[0].result : "CHANGEME"
  description = "Postgres password of the `glitchtip` role on the CLHEAR Aurora cluster"
  lifecycle {
    ignore_changes = [value]
  }
}

resource "aws_ssm_parameter" "glitchtip_secret_key" {
  name  = "/clhear/GLITCHTIP_SECRET_KEY"
  type  = "SecureString"
  value = local.deploy_glitchtip ? random_password.glitchtip_secret[0].result : "CHANGEME"
  lifecycle {
    ignore_changes = [value]
  }
}

# The DSN the web tier and the fleets report to; filled in by the operator after
# creating the CLHEAR project in GlitchTip (Settings → Projects → Client keys).
resource "aws_ssm_parameter" "sentry_dsn" {
  name        = "/clhear/SENTRY_DSN"
  type        = "SecureString"
  value       = "CHANGEME"
  description = "GlitchTip DSN for the CLHEAR project (empty/CHANGEME = error tracking off)"
  lifecycle {
    ignore_changes = [value]
  }
}

resource "aws_security_group" "glitchtip" {
  count       = local.deploy_glitchtip ? 1 : 0
  name        = "${var.name_prefix}-glitchtip"
  vpc_id      = var.existing_vpc_id
  description = "GlitchTip web from the ALB only"

  ingress {
    from_port   = 8000
    to_port     = 8000
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

resource "aws_security_group_rule" "aurora_from_glitchtip" {
  count                    = local.deploy_glitchtip ? 1 : 0
  type                     = "ingress"
  from_port                = 5432
  to_port                  = 5432
  protocol                 = "tcp"
  security_group_id        = aws_security_group.aurora[0].id
  source_security_group_id = aws_security_group.glitchtip[0].id
  description              = "Postgres from GlitchTip"
}

resource "aws_cloudwatch_log_group" "glitchtip" {
  count             = local.deploy_glitchtip ? 1 : 0
  name              = "/ecs/${var.name_prefix}-glitchtip"
  retention_in_days = 30
}

locals {
  glitchtip_env = [
    { name = "GLITCHTIP_DOMAIN", value = "https://${var.glitchtip_hostname}" },
    { name = "DEFAULT_FROM_EMAIL", value = "errors@${var.clhear_hostname}" },
    { name = "EMAIL_URL", value = "smtp+tls://email-smtp.${var.aws_region}.amazonaws.com:587" },
    { name = "REDIS_URL", value = "redis://127.0.0.1:6379/0" },
    { name = "ENABLE_USER_REGISTRATION", value = "False" }, # maintainers are invited, never self-registered
    { name = "ENABLE_ORGANIZATION_CREATION", value = "False" },
    { name = "GLITCHTIP_MAX_EVENT_LIFE_DAYS", value = "90" }, # same retention as the drill evidence
    { name = "CELERY_WORKER_AUTOSCALE", value = "1,2" },
    # Discrete DATABASE_* settings (GlitchTip supports them as an alternative to
    # DATABASE_URL) so the password stays a task secret and never lands in a URL.
    { name = "DATABASE_HOST", value = local.deploy_glitchtip ? aws_rds_cluster.clhear[0].endpoint : "" },
    { name = "DATABASE_PORT", value = "5432" },
    { name = "DATABASE_NAME", value = "glitchtip" },
    { name = "DATABASE_USER", value = "glitchtip" },
  ]
  glitchtip_secrets = [
    { name = "SECRET_KEY", valueFrom = aws_ssm_parameter.glitchtip_secret_key.arn },
    { name = "DATABASE_PASSWORD", valueFrom = aws_ssm_parameter.glitchtip_db_password.arn },
  ]
  glitchtip_log = {
    logDriver = "awslogs"
    options = {
      awslogs-group         = local.deploy_glitchtip ? aws_cloudwatch_log_group.glitchtip[0].name : ""
      awslogs-region        = var.aws_region
      awslogs-stream-prefix = "glitchtip"
    }
  }
}

resource "aws_ecs_task_definition" "glitchtip" {
  count                    = local.deploy_glitchtip ? 1 : 0
  family                   = "${var.name_prefix}-glitchtip"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 1024
  memory                   = 2048
  execution_role_arn       = aws_iam_role.worker_execution.arn

  container_definitions = jsonencode([
    {
      name             = "redis"
      image            = var.discourse_redis_image
      essential        = true
      cpu              = 128
      memory           = 256
      portMappings     = [{ containerPort = 6379, protocol = "tcp" }]
      logConfiguration = local.glitchtip_log
    },
    {
      name             = "web"
      image            = var.glitchtip_image
      essential        = true
      cpu              = 512
      memory           = 1024
      command          = ["./bin/start.sh"]
      environment      = local.glitchtip_env
      secrets          = local.glitchtip_secrets
      portMappings     = [{ containerPort = 8000, protocol = "tcp" }]
      dependsOn        = [{ containerName = "redis", condition = "START" }]
      logConfiguration = local.glitchtip_log
    },
    {
      name             = "worker"
      image            = var.glitchtip_image
      essential        = true
      cpu              = 384
      memory           = 768
      command          = ["./bin/run-celery-with-beat.sh"]
      environment      = local.glitchtip_env
      secrets          = local.glitchtip_secrets
      dependsOn        = [{ containerName = "redis", condition = "START" }]
      logConfiguration = local.glitchtip_log
    },
  ])
}

resource "aws_lb_target_group" "glitchtip" {
  count       = local.deploy_glitchtip && var.existing_alb_listener_arn != "" ? 1 : 0
  name        = "${var.name_prefix}-glitchtip"
  port        = 8000
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = var.existing_vpc_id

  health_check {
    path                = "/_health/"
    matcher             = "200"
    interval            = 30
    healthy_threshold   = 2
    unhealthy_threshold = 5
  }
  deregistration_delay = 30
}

resource "aws_lb_listener_rule" "glitchtip_host" {
  count        = local.deploy_glitchtip && var.existing_alb_listener_arn != "" ? 1 : 0
  listener_arn = var.existing_alb_listener_arn
  priority     = 44

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.glitchtip[0].arn
  }
  condition {
    host_header {
      values = [var.glitchtip_hostname]
    }
  }
}

resource "aws_ecs_service" "glitchtip" {
  count                              = local.deploy_glitchtip ? 1 : 0
  name                               = "${var.name_prefix}-glitchtip"
  cluster                            = local.cluster_arn
  task_definition                    = aws_ecs_task_definition.glitchtip[0].arn
  desired_count                      = 1
  launch_type                        = "FARGATE"
  deployment_minimum_healthy_percent = 0 # one beat scheduler: replace, never run two
  deployment_maximum_percent         = 100
  enable_execute_command             = true
  health_check_grace_period_seconds  = 300

  network_configuration {
    subnets          = var.existing_private_subnet_ids
    security_groups  = [aws_security_group.glitchtip[0].id]
    assign_public_ip = false
  }

  dynamic "load_balancer" {
    for_each = var.existing_alb_listener_arn != "" ? [1] : []
    content {
      target_group_arn = aws_lb_target_group.glitchtip[0].arn
      container_name   = "web"
      container_port   = 8000
    }
  }
}

output "prometheus_workspace_endpoint" {
  value = local.deploy_observability ? aws_prometheus_workspace.clhear[0].prometheus_endpoint : ""
}

output "grafana_endpoint" {
  value = local.deploy_observability ? aws_grafana_workspace.clhear[0].endpoint : ""
}

output "glitchtip_url" {
  value = local.deploy_glitchtip ? "https://${var.glitchtip_hostname}" : ""
}
