# S3 datalake: versioning ON, Object Lock ON at creation (cannot retrofit),
# compliance mode, lifecycle -> IA @30d. Prefixes: public-ok/, restricted/, byol/{user}/.
resource "aws_s3_bucket" "datalake" {
  bucket              = var.datalake_bucket_name
  object_lock_enabled = true
}

resource "aws_s3_bucket_versioning" "datalake" {
  bucket = aws_s3_bucket.datalake.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_object_lock_configuration" "datalake" {
  bucket = aws_s3_bucket.datalake.id
  rule {
    default_retention {
      mode = "COMPLIANCE"
      days = var.object_lock_retention_days
    }
  }
}

resource "aws_s3_bucket_public_access_block" "datalake" {
  bucket                  = aws_s3_bucket.datalake.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "datalake" {
  bucket = aws_s3_bucket.datalake.id
  rule {
    id     = "to-ia-30d"
    status = "Enabled"
    filter {}
    transition {
      days          = 30
      storage_class = "STANDARD_IA"
    }
  }
}

# restricted/ readable only by the worker task role (restricted zone discipline).
resource "aws_s3_bucket_policy" "datalake" {
  bucket = aws_s3_bucket.datalake.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "DenyRestrictedReadExceptWorkers"
        Effect    = "Deny"
        Principal = "*"
        Action    = ["s3:GetObject", "s3:GetObjectVersion"]
        Resource  = "${aws_s3_bucket.datalake.arn}/restricted/*"
        Condition = {
          StringNotLike = {
            "aws:PrincipalArn" = [
              aws_iam_role.worker_task.arn,
              "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root",
            ]
          }
        }
      }
    ]
  })
}

data "aws_caller_identity" "current" {}
