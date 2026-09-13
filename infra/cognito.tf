# HLD v2 §5 identity: a Cognito user pool for public CLHEAR users, with Google
# as a federated IdP (Reg42 staff sign in with their Google Workspace account;
# the public may use Google or email). The web tier verifies the pool's RS256
# id tokens against its JWKS (app/clhear/app_auth.py) and mints the same
# stateless session it uses for magic-link logins, so every mode of the UI
# sees one user identity. SAML (enterprise) is added by item 17 on the same
# pool. API keys per org live in the database (community.api_keys).
#
# Deployed with the web UI (local.deploy_webui); no-op otherwise.

variable "cognito_enabled" {
  description = "Create the Cognito user pool + Google IdP for public sign-in"
  type        = bool
  default     = true
}

locals {
  deploy_cognito = local.deploy_webui && var.cognito_enabled
  cognito_domain = "${var.name_prefix}-auth"
  cognito_callbacks = [
    "https://${var.clhear_hostname}/auth/cognito/callback",
    "http://localhost:8000/auth/cognito/callback",
  ]
  cognito_logouts = [
    "https://${var.clhear_hostname}/",
    "http://localhost:8000/",
  ]
}

resource "aws_cognito_user_pool" "clhear" {
  count = local.deploy_cognito ? 1 : 0
  name  = "${var.name_prefix}-users"

  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]
  mfa_configuration        = "OPTIONAL"

  software_token_mfa_configuration {
    enabled = true
  }

  password_policy {
    minimum_length                   = 12
    require_lowercase                = true
    require_uppercase                = true
    require_numbers                  = true
    require_symbols                  = false
    temporary_password_validity_days = 3
  }

  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }

  # Contributors sign up themselves; no admin-only creation (I9: open by mode).
  admin_create_user_config {
    allow_admin_create_user_only = false
  }

  user_attribute_update_settings {
    attributes_require_verification_before_update = ["email"]
  }

  schema {
    name                = "email"
    attribute_data_type = "String"
    required            = true
    mutable             = true
    string_attribute_constraints {
      min_length = 3
      max_length = 254
    }
  }

  email_configuration {
    email_sending_account = "COGNITO_DEFAULT"
  }

  tags = {
    Project = "clhear"
    Layer   = "L0-identity"
  }
}

resource "aws_cognito_user_pool_domain" "clhear" {
  count        = local.deploy_cognito ? 1 : 0
  domain       = local.cognito_domain
  user_pool_id = aws_cognito_user_pool.clhear[0].id
}

# Google IdP — credentials come from the same SSM parameters the direct
# Google OAuth flow uses (infra/ses.tf); the pool is only wired when they are set.
resource "aws_cognito_identity_provider" "google" {
  count         = local.deploy_cognito && data.aws_ssm_parameter.google_oauth_client_id[0].value != "CHANGEME" ? 1 : 0
  user_pool_id  = aws_cognito_user_pool.clhear[0].id
  provider_name = "Google"
  provider_type = "Google"

  provider_details = {
    client_id                     = data.aws_ssm_parameter.google_oauth_client_id[0].value
    client_secret                 = data.aws_ssm_parameter.google_oauth_client_secret[0].value
    authorize_scopes              = "openid email profile"
    attributes_url                = "https://people.googleapis.com/v1/people/me?personFields="
    attributes_url_add_attributes = "true"
    authorize_url                 = "https://accounts.google.com/o/oauth2/v2/auth"
    oidc_issuer                   = "https://accounts.google.com"
    token_request_method          = "POST"
    token_url                     = "https://www.googleapis.com/oauth2/v4/token"
  }

  attribute_mapping = {
    email    = "email"
    username = "sub"
    name     = "name"
  }

  lifecycle {
    ignore_changes = [provider_details["client_secret"]]
  }
}

resource "aws_cognito_user_pool_client" "web" {
  count        = local.deploy_cognito ? 1 : 0
  name         = "${var.name_prefix}-web"
  user_pool_id = aws_cognito_user_pool.clhear[0].id

  generate_secret                      = false # public client (browser redirect + PKCE)
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes                 = ["openid", "email", "profile"]
  callback_urls                        = local.cognito_callbacks
  logout_urls                          = local.cognito_logouts
  supported_identity_providers         = concat(["COGNITO"], length(aws_cognito_identity_provider.google) > 0 ? ["Google"] : [])
  prevent_user_existence_errors        = "ENABLED"
  enable_token_revocation              = true

  explicit_auth_flows = [
    "ALLOW_REFRESH_TOKEN_AUTH",
    "ALLOW_USER_SRP_AUTH",
  ]

  id_token_validity      = 60
  access_token_validity  = 60
  refresh_token_validity = 30
  token_validity_units {
    id_token      = "minutes"
    access_token  = "minutes"
    refresh_token = "days"
  }

  depends_on = [aws_cognito_identity_provider.google]
}

# Published so the web tier can verify tokens (issuer + JWKS) without any
# secret: pool id, client id and hosted-UI domain are public by design.
resource "aws_ssm_parameter" "cognito_user_pool_id" {
  count = local.deploy_cognito ? 1 : 0
  name  = "/clhear/COGNITO_USER_POOL_ID"
  type  = "String"
  value = aws_cognito_user_pool.clhear[0].id
}

resource "aws_ssm_parameter" "cognito_client_id" {
  count = local.deploy_cognito ? 1 : 0
  name  = "/clhear/COGNITO_CLIENT_ID"
  type  = "String"
  value = aws_cognito_user_pool_client.web[0].id
}

output "cognito_user_pool_id" {
  value = local.deploy_cognito ? aws_cognito_user_pool.clhear[0].id : ""
}

output "cognito_client_id" {
  value = local.deploy_cognito ? aws_cognito_user_pool_client.web[0].id : ""
}

output "cognito_hosted_ui" {
  value = local.deploy_cognito ? "https://${local.cognito_domain}.auth.${var.aws_region}.amazoncognito.com" : ""
}
