# One rule per adapter schedule (HLD §5). Enqueues an AdapterRunRequested job
# message; the clhear-workers service scales from queue depth and runs it.
# Rules ship DISABLED in P0 and are enabled per-adapter as P1–P3 land.
locals {
  adapter_schedules = {
    uk_legislation   = "rate(1 day)"
    eur_lex          = "rate(1 day)"
    govinfo_us       = "rate(7 days)"
    irs_gov          = "rate(7 days)"
    catalog_watchers = "rate(7 days)"
  }
}

resource "aws_cloudwatch_event_rule" "adapter" {
  for_each            = local.adapter_schedules
  name                = "${var.name_prefix}-adapter-${each.key}"
  schedule_expression = each.value
  state               = "DISABLED" # enabled when the adapter lands (P1+)
}

resource "aws_cloudwatch_event_target" "adapter_to_sqs" {
  for_each = local.adapter_schedules
  rule     = aws_cloudwatch_event_rule.adapter[each.key].name
  arn      = aws_sqs_queue.events.arn
  input = jsonencode({
    event_id       = "schedule-${each.key}" # replaced by relay-produced ids for real events
    layer          = "l1"
    kind           = "AdapterRunRequested"
    subject_ref    = each.key
    payload        = { adapter = each.key }
    schema_version = 1
    producer       = "eventbridge"
    ts             = ""
  })
}

resource "aws_sqs_queue_policy" "allow_eventbridge" {
  queue_url = aws_sqs_queue.events.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Principal = { Service = "events.amazonaws.com" }
        Action    = "sqs:SendMessage"
        Resource  = aws_sqs_queue.events.arn
        Condition = {
          ArnLike = { "aws:SourceArn" = "arn:aws:events:${var.aws_region}:${data.aws_caller_identity.current.account_id}:rule/${var.name_prefix}-adapter-*" }
        }
      }
    ]
  })
}
