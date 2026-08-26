# Public Sources Explorer (P1).
# Served at https://clhear.reg42.ai via API Gateway HTTP API + Lambda
# (Function URL is SCP-blocked). The ALB host rule in alb.tf is the later
# reg42-os swap; DNS points here until that listener is wired.
# Enabled only when the deploy artifacts exist (webui_zip_key set by the
# deploy script).

resource "aws_s3_bucket" "deploy" {
  bucket = "${var.name_prefix}-deploy-${data.aws_caller_identity.current.account_id}"
}

resource "aws_s3_bucket_versioning" "deploy" {
  bucket = aws_s3_bucket.deploy.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_public_access_block" "deploy" {
  bucket                  = aws_s3_bucket.deploy.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

variable "webui_zip_key" {
  type    = string
  default = ""
}

variable "webui_zip_sha256" {
  type    = string
  default = ""
}

variable "webui_db_key" {
  type    = string
  default = ""
}

locals {
  deploy_webui = var.webui_zip_key != ""
}

resource "aws_iam_role" "webui" {
  count = local.deploy_webui ? 1 : 0
  name  = "${var.name_prefix}-webui"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Principal = { Service = "lambda.amazonaws.com" }
        Action    = "sts:AssumeRole"
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "webui_logs" {
  count      = local.deploy_webui ? 1 : 0
  role       = aws_iam_role.webui[0].name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "webui_db" {
  count = local.deploy_webui ? 1 : 0
  name  = "${var.name_prefix}-webui-db"
  role  = aws_iam_role.webui[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = "${aws_s3_bucket.deploy.arn}/*"
      }
    ]
  })
}

resource "aws_lambda_function" "webui" {
  count            = local.deploy_webui ? 1 : 0
  function_name    = "${var.name_prefix}-webui"
  role             = aws_iam_role.webui[0].arn
  runtime          = "python3.12"
  handler          = "app.clhear.lambda_web.handler"
  s3_bucket        = aws_s3_bucket.deploy.id
  s3_key           = var.webui_zip_key
  source_code_hash = var.webui_zip_sha256
  memory_size      = 512
  timeout          = 30

  environment {
    variables = {
      CLHEAR_DB_S3_URI          = var.webui_db_key != "" ? "s3://${aws_s3_bucket.deploy.bucket}/${var.webui_db_key}" : ""
      CLHEAR_RELEASES_S3_PREFIX = "s3://${aws_s3_bucket.deploy.bucket}/releases"
      CLHEAR_APP_KEYS           = "os-dev:dev-os-key,safeluance-dev:dev-sl-key,galaxy:galaxy-os-key"
      REG42_CLHEAR_ENABLED      = "true"
    }
  }
}

# Public entry: API Gateway HTTP API -> Lambda. (A plain Function URL is
# blocked by an org-level SCP on lambda:InvokeFunctionUrl in this account.)
resource "aws_apigatewayv2_api" "webui" {
  count         = local.deploy_webui ? 1 : 0
  name          = "${var.name_prefix}-webui"
  protocol_type = "HTTP"
}

resource "aws_apigatewayv2_integration" "webui" {
  count                  = local.deploy_webui ? 1 : 0
  api_id                 = aws_apigatewayv2_api.webui[0].id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.webui[0].invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "webui_default" {
  count     = local.deploy_webui ? 1 : 0
  api_id    = aws_apigatewayv2_api.webui[0].id
  route_key = "$default"
  target    = "integrations/${aws_apigatewayv2_integration.webui[0].id}"
}

resource "aws_apigatewayv2_stage" "webui" {
  count       = local.deploy_webui ? 1 : 0
  api_id      = aws_apigatewayv2_api.webui[0].id
  name        = "$default"
  auto_deploy = true
}

resource "aws_lambda_permission" "webui_apigw" {
  count         = local.deploy_webui ? 1 : 0
  statement_id  = "AllowApiGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.webui[0].function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.webui[0].execution_arn}/*/*"
}

output "webui_url" {
  value = local.deploy_webui ? "https://${var.clhear_hostname}" : null
}

output "webui_api_endpoint" {
  description = "Raw execute-api URL (fallback; public hostname is webui_url)."
  value       = local.deploy_webui ? aws_apigatewayv2_api.webui[0].api_endpoint : null
}
