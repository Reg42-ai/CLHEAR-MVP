# Secrets live in SSM, never in env files (HLD §5). Placeholder values are set
# once here and then owned by the console/CLI — terraform ignores changes.
#
# Inference goes only through Reg42 Infer (I6): no vendor API keys and no
# model-runtime credentials are provisioned for CLHEAR.
resource "aws_ssm_parameter" "infer_token" {
  name        = "/clhear/INFER_TOKEN"
  type        = "SecureString"
  value       = "CHANGEME"
  description = "Reg42 Infer bearer token for the clhear-* employee ids"
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
  value = local.deploy_aurora ? local.aurora_dsn : "CHANGEME"
}
