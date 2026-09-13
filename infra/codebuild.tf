# Worker image build in the account (item 1 fleets). Operators do not need Docker:
# scripts/build_worker_image.sh zips the build context (Dockerfile, requirements,
# app/, migrations/) into the deploy bucket and starts this project, which builds
# the image and pushes it to the clhear-workers ECR repository as :latest plus a
# timestamp tag. The fleets' task definitions reference :latest and pick the new
# image up on their next scale-out.

locals {
  workers_source_key = "build/workers-source.zip"
}

data "aws_iam_policy_document" "codebuild_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["codebuild.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "codebuild" {
  name               = "${var.name_prefix}-codebuild"
  assume_role_policy = data.aws_iam_policy_document.codebuild_assume.json
}

resource "aws_cloudwatch_log_group" "codebuild" {
  name              = "/aws/codebuild/${var.name_prefix}-workers-image"
  retention_in_days = 30
}

resource "aws_iam_role_policy" "codebuild" {
  name = "${var.name_prefix}-codebuild"
  role = aws_iam_role.codebuild.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "Logs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.codebuild.arn}:*"
      },
      {
        Sid      = "Source"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:GetObjectVersion"]
        Resource = "${aws_s3_bucket.deploy.arn}/build/*"
      },
      {
        Sid      = "SourceBucket"
        Effect   = "Allow"
        Action   = ["s3:GetBucketLocation", "s3:ListBucket"]
        Resource = aws_s3_bucket.deploy.arn
      },
      {
        Sid      = "EcrAuth"
        Effect   = "Allow"
        Action   = ["ecr:GetAuthorizationToken"]
        Resource = "*"
      },
      {
        Sid    = "EcrPush"
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability", "ecr:CompleteLayerUpload", "ecr:InitiateLayerUpload",
          "ecr:PutImage", "ecr:UploadLayerPart", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer",
        ]
        Resource = aws_ecr_repository.workers.arn
      },
    ]
  })
}

resource "aws_codebuild_project" "workers_image" {
  name          = "${var.name_prefix}-workers-image"
  description   = "Builds the clhear-workers fleet image from the deploy bucket source zip and pushes it to ECR"
  service_role  = aws_iam_role.codebuild.arn
  build_timeout = 30

  artifacts {
    type = "NO_ARTIFACTS"
  }

  environment {
    compute_type    = "BUILD_GENERAL1_SMALL"
    image           = "aws/codebuild/standard:7.0"
    type            = "LINUX_CONTAINER"
    privileged_mode = true # docker build

    environment_variable {
      name  = "ECR_REPO_URL"
      value = aws_ecr_repository.workers.repository_url
    }
    environment_variable {
      name  = "IMAGE_TAG"
      value = "manual"
    }
  }

  source {
    type     = "S3"
    location = "${aws_s3_bucket.deploy.bucket}/${local.workers_source_key}"
    # The base image is pulled from ECR Public (official Docker library mirror) so
    # the build is not subject to Docker Hub's anonymous pull limits.
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
      group_name = aws_cloudwatch_log_group.codebuild.name
    }
  }
}

output "workers_image_build_project" {
  value = aws_codebuild_project.workers_image.name
}
