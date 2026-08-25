# One rule per adapter schedule (HLD §5). Enqueues an AdapterRunRequested job
# message; the clhear-workers service scales from queue depth and runs it.
# Cron times (UTC) mirror FLEET_SCHEDULES in app/clhear/l1/models.py — the UI
# shows that dictionary, so keep the two in sync.
locals {
  adapter_schedules = {
    uk_legislation   = "cron(20 5 * * ? *)"  # daily 05:20 UTC
    eur_lex          = "cron(40 5 * * ? *)"  # daily 05:40 UTC
    govinfo_us       = "cron(0 6 ? * MON *)" # weekly Mon 06:00 UTC (+ NIST)
    irs_gov          = "cron(20 6 ? * MON *)" # weekly; no-op until P3 adapter
    catalog_watchers = "cron(40 6 ? * MON *)" # weekly; no-op until P3 watchers
  }
}

resource "aws_cloudwatch_event_rule" "adapter" {
  for_each            = local.adapter_schedules
  name                = "${var.name_prefix}-adapter-${each.key}"
  schedule_expression = each.value
  state               = var.schedules_enabled ? "ENABLED" : "DISABLED"
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
