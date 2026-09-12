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

# Workers publish CLHEAR/DailyLlmSpendUsd from the llm_calls ledger.
# Sidecar/GPU Ollama hit their CPU limit (cgroup nr_throttled delta).
# Missing is normal: GPU is dark most of the day; sidecar publishes only when up.
resource "aws_cloudwatch_metric_alarm" "ollama_cpu_throttled" {
  alarm_name          = "${var.name_prefix}-ollama-cpu-throttled"
  namespace           = "CLHEAR"
  metric_name         = "OllamaCpuThrottled"
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 2
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_description   = "Ollama sidecar or GPU container is CPU-throttled (noisy neighbor / undersized limit)."
}

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
      {
        type = "metric", x = 0, y = 6, width = 12, height = 6
        properties = {
          title   = "GPU orphans (must stay 0)"
          region  = var.aws_region
          metrics = [["CLHEAR", "GpuOrphanCount"]]
        }
      },
      {
        type = "metric", x = 12, y = 6, width = 12, height = 6
        properties = {
          title  = "Ollama CPU throttle (sidecar / GPU)"
          region = var.aws_region
          metrics = [
            ["CLHEAR", "OllamaCpuThrottled", "Role", "sidecar"],
            ["CLHEAR", "OllamaCpuThrottled", "Role", "gpu"],
          ]
        }
      },
    ]
  })
}
