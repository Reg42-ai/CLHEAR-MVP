data "aws_iam_policy_document" "ecs_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "worker_execution" {
  name               = "${var.name_prefix}-worker-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

resource "aws_iam_role_policy_attachment" "worker_execution" {
  role       = aws_iam_role.worker_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# clhear_writer-equivalent runtime role: datalake rw (incl. restricted/),
# events queue consume+produce, SSM params read, logs.
resource "aws_iam_role" "worker_task" {
  name               = "${var.name_prefix}-worker-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

resource "aws_iam_role_policy" "worker_task" {
  name = "${var.name_prefix}-worker-task"
  role = aws_iam_role.worker_task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "Datalake"
        Effect   = "Allow"
        Action   = ["s3:PutObject", "s3:GetObject", "s3:GetObjectVersion", "s3:ListBucket"]
        Resource = [aws_s3_bucket.datalake.arn, "${aws_s3_bucket.datalake.arn}/*"]
      },
      {
        Sid      = "SnapshotDb"
        Effect   = "Allow"
        Action   = ["s3:PutObject", "s3:GetObject", "s3:ListBucket"]
        Resource = [aws_s3_bucket.deploy.arn, "${aws_s3_bucket.deploy.arn}/webui/*"]
      },
      {
        Sid    = "EventsQueue"
        Effect = "Allow"
        Action = [
          "sqs:SendMessage", "sqs:ReceiveMessage", "sqs:DeleteMessage",
          "sqs:GetQueueAttributes", "sqs:GetQueueUrl",
        ]
        Resource = [aws_sqs_queue.events.arn, aws_sqs_queue.events_dlq.arn]
      },
      {
        Sid      = "Params"
        Effect   = "Allow"
        Action   = ["ssm:GetParameter", "ssm:GetParameters"]
        Resource = "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/clhear/*"
      },
      {
        Sid      = "Metrics"
        Effect   = "Allow"
        Action   = ["cloudwatch:PutMetricData"]
        Resource = "*"
        Condition = {
          StringEquals = { "cloudwatch:namespace" = "CLHEAR" }
        }
      },
    ]
  })
}
