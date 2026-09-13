# clhear-infer: CLHEAR's private Reg42 Infer router (HLD v2 I6).
#
# CLHEAR is a product, not a workforce seat. Routing it through the shared
# workforce Infer would (a) register it as an employee and (b) charge its
# derivation spend against the employees' company-wide monthly ceiling, which
# Infer enforces over every caller. So CLHEAR runs the same Infer image, pinned
# by digest, with its own catalog (infra/infer-catalog/: only procurement-clean
# Bedrock models, one route per CLHEAR task class, one principal, its own caps),
# its own token secret and its own spend ledger. Reachable only inside the VPC
# at http://infer.<prefix>.local:8000 from the fleet and web-tier security groups.
#
# Image build: scripts/build_worker_image.sh infer (CodeBuild, no local Docker).

locals {
  deploy_infer       = var.infer_private_enabled && local.have_network && var.infer_image != ""
  infer_dns_name     = "infer.${var.name_prefix}.local"
  infer_internal_url = "http://${local.infer_dns_name}:8000/v1"
  # Fleets and the web tier talk to the private router when it exists.
  infer_base_url   = local.deploy_infer ? local.infer_internal_url : var.infer_base_url
  infer_source_key = "build/infer-source.zip"
  deploy_cloud_map = local.deploy_neo4j || local.deploy_infer
}

variable "infer_private_enabled" {
  type        = bool
  default     = true
  description = "Run clhear-infer, CLHEAR's private Infer router, instead of the shared workforce Infer."
}

variable "infer_image" {
  type        = string
  default     = ""
  description = "clhear-infer image (ECR URL:tag). Empty until scripts/build_worker_image.sh infer has pushed one."
}

# --- image -------------------------------------------------------------------

resource "aws_ecr_repository" "infer" {
  name                 = "${var.name_prefix}-infer"
  image_tag_mutability = "MUTABLE"
  force_delete         = true

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_cloudwatch_log_group" "codebuild_infer" {
  name              = "/aws/codebuild/${var.name_prefix}-infer-image"
  retention_in_days = 30
}

# The overlay build pulls the pinned base from the workforce Infer repository and
# pushes to clhear-infer; the shared CodeBuild role gets exactly those two grants.
resource "aws_iam_role_policy" "codebuild_infer" {
  name = "${var.name_prefix}-codebuild-infer"
  role = aws_iam_role.codebuild.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "Logs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.codebuild_infer.arn}:*"
      },
      {
        Sid      = "PullBase"
        Effect   = "Allow"
        Action   = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"]
        Resource = "arn:aws:ecr:${var.aws_region}:${data.aws_caller_identity.current.account_id}:repository/workforce-dev-infer"
      },
      {
        Sid    = "Push"
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability", "ecr:CompleteLayerUpload", "ecr:InitiateLayerUpload",
          "ecr:PutImage", "ecr:UploadLayerPart", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer",
        ]
        Resource = aws_ecr_repository.infer.arn
      },
    ]
  })
}

resource "aws_codebuild_project" "infer_image" {
  name          = "${var.name_prefix}-infer-image"
  description   = "Builds clhear-infer (pinned Reg42 Infer image + CLHEAR catalog) and pushes it to ECR"
  service_role  = aws_iam_role.codebuild.arn
  build_timeout = 20

  artifacts {
    type = "NO_ARTIFACTS"
  }

  environment {
    compute_type    = "BUILD_GENERAL1_SMALL"
    image           = "aws/codebuild/standard:7.0"
    type            = "LINUX_CONTAINER"
    privileged_mode = true

    environment_variable {
      name  = "ECR_REPO_URL"
      value = aws_ecr_repository.infer.repository_url
    }
    environment_variable {
      name  = "IMAGE_TAG"
      value = "manual"
    }
  }

  source {
    type      = "S3"
    location  = "${aws_s3_bucket.deploy.bucket}/${local.infer_source_key}"
    buildspec = <<-YAML
      version: 0.2
      phases:
        pre_build:
          commands:
            - REGISTRY=$${ECR_REPO_URL%%/*}
            - aws ecr get-login-password --region $AWS_DEFAULT_REGION | docker login --username AWS --password-stdin "$REGISTRY"
        build:
          commands:
            - docker build -t "$ECR_REPO_URL:$IMAGE_TAG" -t "$ECR_REPO_URL:latest" .
        post_build:
          commands:
            - docker push "$ECR_REPO_URL:$IMAGE_TAG"
            - docker push "$ECR_REPO_URL:latest"
            - docker inspect --format='{{index .RepoDigests 0}}' "$ECR_REPO_URL:latest"
    YAML
  }

  logs_config {
    cloudwatch_logs {
      group_name = aws_cloudwatch_log_group.codebuild_infer.name
    }
  }
}

# --- identity and secrets ----------------------------------------------------

# HMAC key for CLHEAR's own infer-v2 tokens. Rotating it invalidates every token
# minted against it; re-mint /clhear/INFER_TOKEN afterwards.
resource "random_password" "infer_token_secret" {
  length  = 48
  special = false
}

resource "aws_ssm_parameter" "infer_token_secret" {
  name        = "/clhear/INFER_TOKEN_SECRET"
  type        = "SecureString"
  value       = random_password.infer_token_secret.result
  description = "clhear-infer token signing secret (HMAC-SHA256 for infer-v2 tokens)"
  lifecycle {
    ignore_changes = [value]
  }
}

resource "aws_iam_role" "infer_task" {
  name               = "${var.name_prefix}-infer-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

resource "aws_iam_role_policy" "infer_task" {
  name = "${var.name_prefix}-infer-task"
  role = aws_iam_role.infer_task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "Bedrock"
        Effect = "Allow"
        Action = [
          "bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream",
          "bedrock:Converse", "bedrock:ConverseStream",
        ]
        Resource = "*"
      },
      {
        Sid      = "Params"
        Effect   = "Allow"
        Action   = ["ssm:GetParameter", "ssm:GetParameters"]
        Resource = "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/clhear/*"
      },
    ]
  })
}

# --- network -----------------------------------------------------------------

resource "aws_security_group" "infer" {
  count       = local.have_network ? 1 : 0
  name        = "${var.name_prefix}-infer"
  description = "clhear-infer: HTTP from the layer fleets (and the web tier once it is in the VPC)"
  vpc_id      = var.existing_vpc_id

  ingress {
    from_port       = 8000
    to_port         = 8000
    protocol        = "tcp"
    security_groups = concat(aws_security_group.workers[*].id, aws_security_group.webui[*].id)
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_service_discovery_service" "infer" {
  count = local.deploy_infer ? 1 : 0
  name  = "infer"

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

# --- service -----------------------------------------------------------------

resource "aws_cloudwatch_log_group" "infer" {
  name              = "/ecs/${var.name_prefix}-infer"
  retention_in_days = 30
}

resource "aws_ecs_task_definition" "infer" {
  count                    = local.deploy_infer ? 1 : 0
  family                   = "${var.name_prefix}-infer"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 512
  memory                   = 1024
  execution_role_arn       = aws_iam_role.worker_execution.arn
  task_role_arn            = aws_iam_role.infer_task.arn

  container_definitions = jsonencode([
    {
      name         = "infer"
      image        = var.infer_image
      essential    = true
      portMappings = [{ containerPort = 8000, hostPort = 8000, protocol = "tcp" }]
      environment = [
        { name = "INFER_SERVICE", value = "router" },
        { name = "ZULIP_GLASS_LISTEN", value = "0" }, # router only: no workforce chat listener
        { name = "INFER_TOKEN_REQUIRE_TTL", value = "1" },
        { name = "CATALOG_DIR", value = "/catalog" },
        { name = "MODELS_PATH", value = "/catalog/models.yaml" },
        { name = "POLICY_PATH", value = "/catalog/policy.yaml" },
        { name = "TOOLS_PATH", value = "/catalog/tools.yaml" },
        { name = "EMPLOYEES_PATH", value = "/catalog/employees.yaml" },
        { name = "AWS_DEFAULT_REGION", value = var.aws_region },
        { name = "ENVIRONMENT", value = "clhear-prod" },
        { name = "PGSSLMODE", value = "require" },
        { name = "APP_BASE_URL", value = local.infer_internal_url },
      ]
      # Spend ledger: CLHEAR's Aurora (database `infer`) once it exists; memory-only before
      # that, which Infer logs at startup. The daily/monthly caps are in the catalog.
      secrets = concat(
        [{ name = "INFER_TOKEN_SECRET", valueFrom = aws_ssm_parameter.infer_token_secret.arn }],
        local.record_on_aurora ? [{ name = "INFER_DATABASE_URL", valueFrom = aws_ssm_parameter.infer_database_url.arn }] : [],
      )
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.infer.name
          awslogs-region        = var.aws_region
          awslogs-stream-prefix = "infer"
        }
      }
    }
  ])
}

resource "aws_ssm_parameter" "infer_database_url" {
  name        = "/clhear/INFER_DATABASE_URL"
  type        = "SecureString"
  value       = "CHANGEME"
  description = "clhear-infer spend ledger DSN (database `infer` on CLHEAR's Aurora); set at the Aurora cutover"
  lifecycle {
    ignore_changes = [value]
  }
}

resource "aws_ecs_service" "infer" {
  count           = local.deploy_infer ? 1 : 0
  name            = "${var.name_prefix}-infer"
  cluster         = aws_ecs_cluster.clhear[0].id
  task_definition = aws_ecs_task_definition.infer[0].arn
  desired_count   = 1
  launch_type     = "FARGATE" # on-demand: the router must not be reclaimed mid-derivation

  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 100

  network_configuration {
    subnets          = var.existing_private_subnet_ids
    security_groups  = [aws_security_group.infer[0].id]
    assign_public_ip = var.worker_assign_public_ip # Bedrock and ECR are reached over the public endpoints
  }

  service_registries {
    registry_arn = aws_service_discovery_service.infer[0].arn
  }
}

output "infer_internal_url" {
  value = local.deploy_infer ? local.infer_internal_url : ""
}
