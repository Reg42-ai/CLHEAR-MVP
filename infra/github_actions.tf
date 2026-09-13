# GitHub Actions → AWS for the release and DR-drill workflows (HLD v2 §3 release
# pipeline; item 17 DR). Keyless: the workflows assume this role through GitHub's
# OIDC provider, so no long-lived access key ever lands in repository secrets.
# The provider itself already exists in the account (shared with other Reg42 repos)
# and is only read here.
#
# Workflow secret to set once: AWS_RELEASE_ROLE_ARN = output github_release_role_arn.

variable "github_repository" {
  type        = string
  default     = "Reg42-ai/CLHEAR-MVP"
  description = "owner/name of the repository whose workflows may assume the release role"
}

variable "github_actions_enabled" {
  type    = bool
  default = true
}

data "aws_iam_openid_connect_provider" "github" {
  count = var.github_actions_enabled ? 1 : 0
  url   = "https://token.actions.githubusercontent.com"
}

resource "aws_iam_role" "github_release" {
  count = var.github_actions_enabled ? 1 : 0
  name  = "${var.name_prefix}-github-release"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = data.aws_iam_openid_connect_provider.github[0].arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = { "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com" }
        # Any ref of this repository: release.yml runs on main; dr_drill.yml and manual
        # dispatches may run from a branch while a release is being rehearsed.
        StringLike = { "token.actions.githubusercontent.com:sub" = "repo:${var.github_repository}:*" }
      }
    }]
  })
}

resource "aws_iam_role_policy" "github_release" {
  count = var.github_actions_enabled ? 1 : 0
  name  = "${var.name_prefix}-github-release"
  role  = aws_iam_role.github_release[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(
      [
        {
          # release.yml uploads the signed release; dr_drill.yml restores the latest one.
          Sid      = "Releases"
          Effect   = "Allow"
          Action   = ["s3:PutObject", "s3:GetObject", "s3:GetObjectVersion"]
          Resource = ["${aws_s3_bucket.deploy.arn}/releases/*", "${aws_s3_bucket.deploy.arn}/webui/*"]
        },
        {
          Sid      = "ListDeploy"
          Effect   = "Allow"
          Action   = ["s3:ListBucket", "s3:GetBucketLocation"]
          Resource = [aws_s3_bucket.deploy.arn]
        },
        {
          # DR drill: read the replication rule on the datalake and sample the replica.
          Sid      = "DatalakeReplicationRead"
          Effect   = "Allow"
          Action   = ["s3:GetReplicationConfiguration", "s3:GetBucketVersioning", "s3:ListBucket"]
          Resource = [aws_s3_bucket.datalake.arn]
        },
        {
          Sid      = "DrDrillMetric"
          Effect   = "Allow"
          Action   = ["cloudwatch:PutMetricData"]
          Resource = "*"
          Condition = {
            StringEquals = { "cloudwatch:namespace" = "CLHEAR" }
          }
        },
      ],
      var.replication_enabled ? [{
        Sid      = "ReplicaRead"
        Effect   = "Allow"
        Action   = ["s3:GetReplicationConfiguration", "s3:GetBucketVersioning", "s3:ListBucket", "s3:GetObject", "s3:GetObjectVersion"]
        Resource = [aws_s3_bucket.datalake_replica[0].arn, "${aws_s3_bucket.datalake_replica[0].arn}/*"]
      }] : [],
    )
  })
}

output "github_release_role_arn" {
  value       = var.github_actions_enabled ? aws_iam_role.github_release[0].arn : ""
  description = "Set as the AWS_RELEASE_ROLE_ARN repository secret"
}
