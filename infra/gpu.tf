# Nightly ephemeral GPU: g6.xlarge spot, VPC-internal, S3 Ollama cache,
# CloudWatch orphan alarm (instances older than 5h / GpuOrphanCount > 0).
# Owner may need a g6 spot vCPU quota increase.

variable "gpu_enabled" {
  type    = bool
  default = true
}

variable "ollama_sidecar_enabled" {
  type        = bool
  default     = false
  description = "Attach an Ollama CPU sidecar to the worker task (needs 4 vCPU / 16 GB)."
}

resource "aws_s3_object" "ollama_cache_prefix" {
  bucket  = aws_s3_bucket.deploy.bucket
  key     = "ollama-models/.keep"
  content = "ollama model cache — nightly GPU restores from here so weights are not re-downloaded"
}

data "aws_iam_policy_document" "gpu_ec2_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "gpu_instance" {
  name               = "${var.name_prefix}-gpu-instance"
  assume_role_policy = data.aws_iam_policy_document.gpu_ec2_assume.json
}

resource "aws_iam_role_policy" "gpu_instance" {
  name = "${var.name_prefix}-gpu-instance"
  role = aws_iam_role.gpu_instance.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "OllamaCache"
        Effect = "Allow"
        Action = ["s3:GetObject", "s3:PutObject", "s3:ListBucket"]
        Resource = [
          aws_s3_bucket.deploy.arn,
          "${aws_s3_bucket.deploy.arn}/ollama-models/*",
        ]
      }
    ]
  })
}

resource "aws_iam_instance_profile" "gpu" {
  name = "${var.name_prefix}-gpu"
  role = aws_iam_role.gpu_instance.name
}

resource "aws_security_group" "gpu" {
  count       = local.have_network ? 1 : 0
  name        = "${var.name_prefix}-gpu"
  vpc_id      = var.existing_vpc_id
  description = "Ollama GPU - no public ingress; workers may reach :11434"

  ingress {
    description     = "Ollama from workers"
    from_port       = 11434
    to_port         = 11434
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

resource "aws_cloudwatch_metric_alarm" "gpu_orphan" {
  alarm_name          = "${var.name_prefix}-gpu-orphan"
  namespace           = "CLHEAR"
  metric_name         = "GpuOrphanCount"
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_description   = "A CLHEAR GPU instance has been running longer than 5h (orphan guard)."
}

output "gpu_security_group_id" {
  value = try(aws_security_group.gpu[0].id, "")
}

output "gpu_instance_profile" {
  value = aws_iam_instance_profile.gpu.name
}

output "ollama_model_cache_s3" {
  value = "s3://${aws_s3_bucket.deploy.bucket}/ollama-models"
}
