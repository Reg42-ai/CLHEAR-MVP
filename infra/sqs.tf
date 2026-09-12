# The single event plane (HLD §5).
resource "aws_sqs_queue" "events_dlq" {
  name                      = "${var.name_prefix}-events-dlq"
  message_retention_seconds = 1209600 # 14 days
}

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
