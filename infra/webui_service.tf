# Long-lived web tier for the explorer and /v1 (replaces the Lambda that
# downloaded the viewer snapshot on every cold start). API Gateway reaches the
# tasks through a VPC link and Cloud Map; the Lambda integration stays defined
# so the $default route can be switched back in one update.
#
# Terraform owns the scaffolding. scripts/web_service.py registers the task
# definition revisions (image digest, environment copied from the verified
# viewer configuration, secrets by SSM reference) and scales the service, so
# the task definition and desired count are ignored here after creation.

variable "web_service_enabled" {
  type    = bool
  default = false
}

variable "web_service_subnet_ids" {
  type    = list(string)
  default = []
}

locals {
  deploy_web_service  = var.web_service_enabled && local.have_network
  web_service_subnets = length(var.web_service_subnet_ids) > 0 ? var.web_service_subnet_ids : var.existing_private_subnet_ids
  web_port            = 8080
  deploy_bucket_name  = "${var.name_prefix}-deploy-${data.aws_caller_identity.current.account_id}"
}

data "aws_apigatewayv2_apis" "webui" {
  count         = local.deploy_web_service ? 1 : 0
  name          = "${var.name_prefix}-webui"
  protocol_type = "HTTP"
}

data "aws_sqs_queue" "l0" {
  count = local.deploy_web_service ? 1 : 0
  name  = "${var.name_prefix}-fleet-l0"
}

resource "aws_cloudwatch_log_group" "web_service" {
  count             = local.deploy_web_service ? 1 : 0
  name              = "/ecs/${var.name_prefix}-webui-service"
  retention_in_days = 30
}

resource "aws_iam_role" "web_service_task" {
  count              = local.deploy_web_service ? 1 : 0
  name               = "${var.name_prefix}-webui-service-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

resource "aws_iam_role_policy" "web_service_task" {
  count = local.deploy_web_service ? 1 : 0
  name  = "${var.name_prefix}-webui-service-task"
  role  = aws_iam_role.web_service_task[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # Viewer snapshots, operator-access control, releases and demo status.
        Effect = "Allow"
        Action = ["s3:GetObject"]
        Resource = [
          "arn:aws:s3:::${local.deploy_bucket_name}/webui/*",
          "arn:aws:s3:::${local.deploy_bucket_name}/releases/*",
        ]
      },
      {
        # Presigned release downloads are signed by this role.
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = "arn:aws:s3:::${local.deploy_bucket_name}"
        Condition = {
          StringLike = { "s3:prefix" = ["webui/*", "releases/*"] }
        }
      },
      {
        # Community write path: the read-only web tier enqueues ops for L0.
        Effect   = "Allow"
        Action   = ["sqs:SendMessage"]
        Resource = data.aws_sqs_queue.l0[0].arn
      },
      {
        Effect   = "Allow"
        Action   = ["ses:SendEmail"]
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = ["ssm:GetParameter", "ssm:GetParameters"]
        Resource = "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/clhear/*"
      },
    ]
  })
}

resource "aws_security_group" "web_vpc_link" {
  count       = local.deploy_web_service ? 1 : 0
  name        = "${var.name_prefix}-webui-vpc-link"
  description = "API Gateway VPC link to the web tasks"
  vpc_id      = var.existing_vpc_id
  egress {
    from_port   = local.web_port
    to_port     = local.web_port
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "web_service" {
  count       = local.deploy_web_service ? 1 : 0
  name        = "${var.name_prefix}-webui-service"
  description = "Web tasks: ingress only from the API Gateway VPC link"
  vpc_id      = var.existing_vpc_id
  ingress {
    from_port       = local.web_port
    to_port         = local.web_port
    protocol        = "tcp"
    security_groups = [aws_security_group.web_vpc_link[0].id]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_service_discovery_private_dns_namespace" "web" {
  count = local.deploy_web_service ? 1 : 0
  name  = "${var.name_prefix}.internal"
  vpc   = var.existing_vpc_id
}

resource "aws_service_discovery_service" "web" {
  count = local.deploy_web_service ? 1 : 0
  name  = "webui"
  dns_config {
    namespace_id   = aws_service_discovery_private_dns_namespace.web[0].id
    routing_policy = "MULTIVALUE"
    dns_records {
      type = "SRV"
      ttl  = 10
    }
  }
  health_check_custom_config {
    failure_threshold = 1
  }
}

# Bootstrap revision only; scripts/web_service.py registers the served ones.
resource "aws_ecs_task_definition" "web_service" {
  count                    = local.deploy_web_service ? 1 : 0
  family                   = "${var.name_prefix}-webui-service"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 2048
  memory                   = 8192
  execution_role_arn       = aws_iam_role.worker_execution.arn
  task_role_arn            = aws_iam_role.web_service_task[0].arn
  ephemeral_storage {
    size_in_gib = 50
  }
  container_definitions = jsonencode([
    {
      name         = "web"
      image        = var.worker_image
      essential    = true
      entryPoint   = ["python", "-m", "app.clhear.web_server"]
      portMappings = [{ containerPort = local.web_port, protocol = "tcp" }]
      environment = [
        { name = "AWS_REGION", value = var.aws_region },
        { name = "CLHEAR_DB_LOCAL_PATH", value = "/tmp/clhear.db" },
      ]
      healthCheck = {
        command     = ["CMD", "python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:${local.web_port}/api/clhear/ready', timeout=4).status == 200 else 1)"]
        interval    = 15
        timeout     = 5
        retries     = 3
        startPeriod = 300
      }
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.web_service[0].name
          awslogs-region        = var.aws_region
          awslogs-stream-prefix = "web"
        }
      }
    }
  ])
}

resource "aws_ecs_service" "web_service" {
  count                              = local.deploy_web_service ? 1 : 0
  name                               = "${var.name_prefix}-webui-service"
  cluster                            = local.cluster_arn
  task_definition                    = aws_ecs_task_definition.web_service[0].arn
  desired_count                      = 0
  launch_type                        = "FARGATE"
  health_check_grace_period_seconds  = 0
  enable_execute_command             = false
  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200
  network_configuration {
    subnets          = local.web_service_subnets
    security_groups  = [aws_security_group.web_service[0].id]
    assign_public_ip = var.worker_assign_public_ip
  }
  service_registries {
    registry_arn   = aws_service_discovery_service.web[0].arn
    container_name = "web"
    container_port = local.web_port
  }
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }
  lifecycle {
    ignore_changes = [task_definition, desired_count]
  }
}

resource "aws_apigatewayv2_vpc_link" "web" {
  count              = local.deploy_web_service ? 1 : 0
  name               = "${var.name_prefix}-webui"
  subnet_ids         = local.web_service_subnets
  security_group_ids = [aws_security_group.web_vpc_link[0].id]
}

resource "aws_apigatewayv2_integration" "web_service" {
  count                  = local.deploy_web_service ? 1 : 0
  api_id                 = tolist(data.aws_apigatewayv2_apis.webui[0].ids)[0]
  integration_type       = "HTTP_PROXY"
  integration_method     = "ANY"
  connection_type        = "VPC_LINK"
  connection_id          = aws_apigatewayv2_vpc_link.web[0].id
  integration_uri        = aws_service_discovery_service.web[0].arn
  payload_format_version = "1.0"
  timeout_milliseconds   = 30000
}

# The web tier reaches Aurora only as clhear_web (identity tables); see
# migrations/m0037_identity.py.
variable "aurora_security_group_id" {
  type    = string
  default = ""
}

resource "aws_vpc_security_group_ingress_rule" "aurora_from_web_service" {
  count                        = local.deploy_web_service && var.aurora_security_group_id != "" ? 1 : 0
  security_group_id            = var.aurora_security_group_id
  referenced_security_group_id = aws_security_group.web_service[0].id
  from_port                    = 5432
  to_port                      = 5432
  ip_protocol                  = "tcp"
  description                  = "CLHEAR web tier identity store"
}

output "web_service_integration_id" {
  value = local.deploy_web_service ? aws_apigatewayv2_integration.web_service[0].id : null
}
