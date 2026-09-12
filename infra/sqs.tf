# Event plane (HLD v2 §3): one SQS queue per layer fleet, fed by EventBridge
# rules on the `clhear` bus (clhear.<layer>.<event>) and by schedules.
resource "aws_sqs_queue" "events_dlq" {
  name                      = "${var.name_prefix}-events-dlq"
  message_retention_seconds = 1209600 # 14 days
}

# L1 fleet queue (kept under its original name: schedules, the web app and the
# running service already point at it).
resource "aws_sqs_queue" "events" {
  name = "${var.name_prefix}-events"
  # A daily adapter run can take 15–40 minutes (dozens of sources through
  # the fidelity gate). Must exceed that so EventBridge messages aren't
  # redelivered mid-ingest.
  visibility_timeout_seconds = 3600
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.events_dlq.arn
    maxReceiveCount     = 5
  })
}

resource "aws_sqs_queue" "fleet" {
  for_each                   = { for k, v in local.fleets : k => v if k != "l1" }
  name                       = "${var.name_prefix}-fleet-${each.key}"
  visibility_timeout_seconds = 1800
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.events_dlq.arn
    maxReceiveCount     = 5
  })
}

# EventBridge (schedules + bus rules) may enqueue on every fleet queue.
resource "aws_sqs_queue_policy" "allow_eventbridge" {
  for_each  = local.fleet_queue
  queue_url = each.value.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Principal = { Service = "events.amazonaws.com" }
        Action    = "sqs:SendMessage"
        Resource  = each.value.arn
        Condition = {
          ArnLike = { "aws:SourceArn" = "arn:aws:events:${var.aws_region}:${data.aws_caller_identity.current.account_id}:rule/*/${var.name_prefix}-*" }
        }
      },
      {
        Effect    = "Allow"
        Principal = { Service = "events.amazonaws.com" }
        Action    = "sqs:SendMessage"
        Resource  = each.value.arn
        Condition = {
          ArnLike = { "aws:SourceArn" = "arn:aws:events:${var.aws_region}:${data.aws_caller_identity.current.account_id}:rule/${var.name_prefix}-*" }
        }
      }
    ]
  })
}
