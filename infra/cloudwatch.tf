# Alarms: DLQ>0, spend>cap, missed schedules, any layer below its gate.
resource "aws_cloudwatch_metric_alarm" "dlq_not_empty" {
  alarm_name          = "${var.name_prefix}-dlq-not-empty"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = aws_sqs_queue.events_dlq.name }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
}

# Honest-schedule gate: the nightly fleet publishes how many scheduled
# sources had NO run attempt in 24h. Anything above zero is a broken promise.
resource "aws_cloudwatch_metric_alarm" "schedule_missed" {
  alarm_name          = "${var.name_prefix}-schedule-missed-sources"
  namespace           = "CLHEAR"
  metric_name         = "ScheduleMissedSources"
  statistic           = "Maximum"
  period              = 86400
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "breaching" # no metric = the nightly job itself did not run
}

# Evals gate publication (I10): gates.freeze_below_gate publishes
# CLHEAR/LayerBelowGate{Layer} when a layer's latest suites fail.
resource "aws_cloudwatch_metric_alarm" "layer_below_gate" {
  for_each            = toset(["L1", "L2", "L3", "L4", "L5", "L6", "L7", "L8"])
  alarm_name          = "${var.name_prefix}-${lower(each.key)}-below-gate"
  namespace           = "CLHEAR"
  metric_name         = "LayerBelowGate"
  dimensions          = { Layer = each.key }
  statistic           = "Maximum"
  period              = 3600
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_description   = "${each.key} dropped below its publication gate; release publication for the layer is frozen."
}

# Workers publish CLHEAR/DailyLlmSpendUsd from the llm_calls ledger (Infer usage).
resource "aws_cloudwatch_metric_alarm" "llm_spend_over_cap" {
  alarm_name          = "${var.name_prefix}-llm-spend-over-cap"
  namespace           = "CLHEAR"
  metric_name         = "DailyLlmSpendUsd"
  statistic           = "Maximum"
  period              = 3600
  evaluation_periods  = 1
  threshold           = 100
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
}

resource "aws_cloudwatch_dashboard" "clhear" {
  dashboard_name = var.name_prefix
  dashboard_body = jsonencode({
    widgets = [
      {
        type = "metric", x = 0, y = 0, width = 12, height = 6
        properties = {
          title  = "Fleet queue depth (per layer) / DLQ"
          region = var.aws_region
          metrics = concat(
            [for k, q in local.fleet_queue : ["AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", q.name, { label = k }]],
            [["AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", aws_sqs_queue.events_dlq.name, { label = "dlq" }]],
          )
        }
      },
      {
        type = "metric", x = 12, y = 0, width = 12, height = 6
        properties = {
          title   = "Daily LLM spend via Infer (USD)"
          region  = var.aws_region
          metrics = [["CLHEAR", "DailyLlmSpendUsd"]]
        }
      },
      {
        type = "metric", x = 0, y = 6, width = 12, height = 6
        properties = {
          title   = "Layers below gate (must stay 0)"
          region  = var.aws_region
          metrics = [for l in ["L1", "L2", "L3", "L4", "L5", "L6", "L7", "L8"] : ["CLHEAR", "LayerBelowGate", "Layer", l]]
        }
      },
      {
        type = "metric", x = 12, y = 6, width = 12, height = 6
        properties = {
          title   = "Scheduled sources missed (24h)"
          region  = var.aws_region
          metrics = [["CLHEAR", "ScheduleMissedSources"]]
        }
      },
    ]
  })
}
