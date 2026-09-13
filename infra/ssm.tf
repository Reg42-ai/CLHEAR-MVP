# Secrets live in SSM, never in env files (HLD §5). Placeholder values are set
# once here and then owned by the console/CLI — terraform ignores changes.
#
# Inference goes only through Reg42 Infer (I6): no vendor API keys and no
# model-runtime credentials are provisioned for CLHEAR.
resource "aws_ssm_parameter" "infer_token" {
  name        = "/clhear/INFER_TOKEN"
  type        = "SecureString"
  value       = "CHANGEME"
  description = "Reg42 Infer bearer token for the clhear employee (infer-v2, expiring; re-mint before expiry)"
  lifecycle {
    ignore_changes = [value]
  }
}

# beehiiv change-digest newsletter (HLD v2 §6 community). "CHANGEME" = hooks inert;
# the operator sets both once the publication exists (app/clhear/platform/newsletter.py).
resource "aws_ssm_parameter" "beehiiv_api_key" {
  name        = "/clhear/BEEHIIV_API_KEY"
  type        = "SecureString"
  value       = "CHANGEME"
  description = "beehiiv API key for the CLHEAR change digest"
  lifecycle {
    ignore_changes = [value]
  }
}

resource "aws_ssm_parameter" "beehiiv_publication_id" {
  name        = "/clhear/BEEHIIV_PUBLICATION_ID"
  type        = "String"
  value       = "CHANGEME"
  description = "beehiiv publication id (pub_…) for the CLHEAR change digest"
  lifecycle {
    ignore_changes = [value]
  }
}

resource "aws_ssm_parameter" "github_deploy_token" {
  name  = "/clhear/GITHUB_DEPLOY_TOKEN"
  type  = "SecureString"
  value = "CHANGEME"
  lifecycle {
    ignore_changes = [value]
  }
}

# Aurora DSN when the record is deployed; otherwise a placeholder the operator
# fills in (snapshot mode keeps SQLite in S3 and ignores it).
resource "aws_ssm_parameter" "database_url" {
  name  = var.database_url_ssm_param
  type  = "SecureString"
  value = local.record_on_aurora ? local.aurora_dsn : "CHANGEME"
}
