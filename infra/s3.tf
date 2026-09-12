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

# --- Cross-region replication (DR). Object Lock replicates retention with
# --- the object, so the replica is as immutable as the source.
resource "aws_s3_bucket" "datalake_replica" {
  count               = var.replication_enabled ? 1 : 0
  provider            = aws.replica
  bucket              = "${var.datalake_bucket_name}-replica"
  object_lock_enabled = true
}

resource "aws_s3_bucket_versioning" "datalake_replica" {
  count    = var.replication_enabled ? 1 : 0
  provider = aws.replica
  bucket   = aws_s3_bucket.datalake_replica[0].id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_public_access_block" "datalake_replica" {
  count                   = var.replication_enabled ? 1 : 0
  provider                = aws.replica
  bucket                  = aws_s3_bucket.datalake_replica[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

data "aws_iam_policy_document" "s3_replication_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["s3.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "datalake_replication" {
  count              = var.replication_enabled ? 1 : 0
  name               = "${var.name_prefix}-datalake-replication"
  assume_role_policy = data.aws_iam_policy_document.s3_replication_assume.json
}

resource "aws_iam_role_policy" "datalake_replication" {
  count = var.replication_enabled ? 1 : 0
  name  = "${var.name_prefix}-datalake-replication"
  role  = aws_iam_role.datalake_replication[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:GetReplicationConfiguration", "s3:ListBucket"]
        Resource = aws_s3_bucket.datalake.arn
      },
      {
        Effect = "Allow"
        Action = [
          "s3:GetObjectVersionForReplication", "s3:GetObjectVersionAcl",
          "s3:GetObjectVersionTagging", "s3:GetObjectRetention", "s3:GetObjectLegalHold",
        ]
        Resource = "${aws_s3_bucket.datalake.arn}/*"
      },
      {
        Effect   = "Allow"
        Action   = ["s3:ReplicateObject", "s3:ReplicateDelete", "s3:ReplicateTags", "s3:ObjectOwnerOverrideToBucketOwner"]
        Resource = "${aws_s3_bucket.datalake_replica[0].arn}/*"
      }
    ]
  })
}

resource "aws_s3_bucket_replication_configuration" "datalake" {
  count      = var.replication_enabled ? 1 : 0
  depends_on = [aws_s3_bucket_versioning.datalake, aws_s3_bucket_versioning.datalake_replica]
  bucket     = aws_s3_bucket.datalake.id
  role       = aws_iam_role.datalake_replication[0].arn

  rule {
    id     = "all-to-replica"
    status = "Enabled"
    filter {}
    delete_marker_replication {
      status = "Disabled" # I2: nothing is deleted; never propagate a delete marker
    }
    destination {
      bucket        = aws_s3_bucket.datalake_replica[0].arn
      storage_class = "STANDARD_IA"
      replication_time {
        status = "Enabled"
        time {
          minutes = 15
        }
      }
      metrics {
        status = "Enabled"
        event_threshold {
          minutes = 15
        }
      }
    }
  }
}
