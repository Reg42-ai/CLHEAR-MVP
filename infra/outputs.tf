output "datalake_bucket" {
  value = aws_s3_bucket.datalake.bucket
}

output "events_queue_url" {
  value = aws_sqs_queue.events.url
}

output "events_dlq_url" {
  value = aws_sqs_queue.events_dlq.url
}

output "worker_task_role_arn" {
  value = aws_iam_role.worker_task.arn
}

output "ecs_cluster_arn" {
  value = local.cluster_arn
}
