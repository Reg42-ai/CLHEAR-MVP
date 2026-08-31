# Secrets live in SSM, never in env files (HLD §5). Values are placeholders;
# set the real values in the console/CLI — terraform ignores changes.
resource "aws_ssm_parameter" "ollama_api_key" {
  name  = "/clhear/OLLAMA_API_KEY"
  type  = "SecureString"
  value = "CHANGEME"
  lifecycle {
    ignore_changes = [value]
  }
}

resource "aws_ssm_parameter" "anthropic_api_key" {
  name  = "/clhear/ANTHROPIC_API_KEY"
  type  = "SecureString"
  value = "CHANGEME"
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

resource "aws_ssm_parameter" "openai_api_key" {
  name  = "/clhear/OPENAI_API_KEY"
  type  = "SecureString"
  value = "CHANGEME"
  lifecycle {
    ignore_changes = [value]
  }
}

resource "aws_ssm_parameter" "xai_api_key" {
  name  = "/clhear/XAI_API_KEY"
  type  = "SecureString"
  value = "CHANGEME"
  lifecycle {
    ignore_changes = [value]
  }
}

resource "aws_ssm_parameter" "database_url" {
  name  = var.database_url_ssm_param
  type  = "SecureString"
  value = "CHANGEME"
  lifecycle {
    ignore_changes = [value]
  }
}
