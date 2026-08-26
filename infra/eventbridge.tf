# One rule per adapter schedule (HLD §5). Enqueues an AdapterRunRequested job
# message; the clhear-workers service scales from queue depth and runs it.
# Cron times (UTC) mirror FLEET_SCHEDULES in app/clhear/l1/models.py — the UI
# shows that dictionary, so keep the two in sync.
locals {
  # One rule per adapter key in app/clhear/l1/models.py FLEET_SCHEDULES.
  # catalog_watchers / "P3 reserved no-op" lanes are gone — every key has a real adapter.
  adapter_schedules = {
    uk_legislation   = "cron(0 0 * * ? *)"
    eur_lex          = "cron(0 0 * * ? *)"
    govinfo_us       = "cron(0 0 * * ? *)"
    fca_handbook     = "cron(0 0 * * ? *)"
    au_legislation   = "cron(0 0 * * ? *)"
    sg_legislation   = "cron(0 0 * * ? *)"
    finra            = "cron(0 0 * * ? *)"
    adgm             = "cron(0 0 * * ? *)"
    nydfs            = "cron(0 0 * * ? *)"
    nasdaq           = "cron(0 0 * * ? *)"
    malta            = "cron(0 0 * * ? *)"
    uae              = "cron(0 0 * * ? *)"
    cysec            = "cron(0 0 * * ? *)"
    mas              = "cron(0 0 * * ? *)"
    fatf             = "cron(0 0 * * ? *)"
    wolfsberg        = "cron(0 0 * * ? *)"
    irs_gov          = "cron(0 0 * * ? *)"
    lists            = "cron(0 0 * * ? *)"
    overlay          = "cron(0 0 * * ? *)"
    restricted_file  = "cron(0 0 * * ? *)"
    seychelles       = "cron(0 0 * * ? *)"
    gibraltar        = "cron(0 0 * * ? *)"
    israel           = "cron(0 0 * * ? *)"
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
