# Worker image lives in its own ECR repo until the package moves into the
# reg42-os image (HLD §5 note on shared images).
resource "aws_ecr_repository" "workers" {
  name                 = "${var.name_prefix}-workers"
  image_tag_mutability = "MUTABLE"
  force_delete         = true

  image_scanning_configuration {
    scan_on_push = true
  }
}

output "workers_ecr_url" {
  value = aws_ecr_repository.workers.repository_url
}
