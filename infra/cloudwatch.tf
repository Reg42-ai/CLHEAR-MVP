# Alarms: DLQ>0, spend>cap, freshness SLA breach (freshness metric lands with P1 adapters).
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

# Workers publish CLHEAR/DailyLlmSpendUsd from the llm_calls ledger.
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
          title  = "Events queue depth / DLQ"
          region = var.aws_region
          metrics = [
            ["AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", aws_sqs_queue.events.name],
            ["AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", aws_sqs_queue.events_dlq.name],
          ]
        }
      },
      {
        type = "metric", x = 12, y = 0, width = 12, height = 6
        properties = {
          title   = "Daily LLM spend (USD)"
          region  = var.aws_region
          metrics = [["CLHEAR", "DailyLlmSpendUsd"]]
        }
      },
      # P1+: crawl freshness per source, eval trend.
    ]
  })
}
