# Public hostname for the Sources Explorer (HLD §5: clhear.reg42.ai).
# ACM DNS-validated in the existing reg42.ai zone; HTTP API custom domain
# aliases to it. The execute-api URL stays up as a fallback.

locals {
  clhear_zone_name = join(".", slice(split(".", var.clhear_hostname), 1, length(split(".", var.clhear_hostname))))
}

data "aws_route53_zone" "clhear" {
  count        = local.deploy_webui ? 1 : 0
  name         = "${local.clhear_zone_name}."
  private_zone = false
}

resource "aws_acm_certificate" "clhear" {
  count             = local.deploy_webui ? 1 : 0
  domain_name       = var.clhear_hostname
  validation_method = "DNS"

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_route53_record" "clhear_cert" {
  for_each = local.deploy_webui ? {
    for dvo in aws_acm_certificate.clhear[0].domain_validation_options : dvo.domain_name => {
      name   = dvo.resource_record_name
      record = dvo.resource_record_value
      type   = dvo.resource_record_type
    }
  } : {}

  allow_overwrite = true
  name            = each.value.name
  records         = [each.value.record]
  ttl             = 60
  type            = each.value.type
  zone_id         = data.aws_route53_zone.clhear[0].zone_id
}

resource "aws_acm_certificate_validation" "clhear" {
  count                   = local.deploy_webui ? 1 : 0
  certificate_arn         = aws_acm_certificate.clhear[0].arn
  validation_record_fqdns = [for record in aws_route53_record.clhear_cert : record.fqdn]
}

resource "aws_apigatewayv2_domain_name" "webui" {
  count       = local.deploy_webui ? 1 : 0
  domain_name = var.clhear_hostname

  domain_name_configuration {
    certificate_arn = aws_acm_certificate_validation.clhear[0].certificate_arn
    endpoint_type   = "REGIONAL"
    security_policy = "TLS_1_2"
  }
}

resource "aws_apigatewayv2_api_mapping" "webui" {
  count       = local.deploy_webui ? 1 : 0
  api_id      = aws_apigatewayv2_api.webui[0].id
  domain_name = aws_apigatewayv2_domain_name.webui[0].id
  stage       = aws_apigatewayv2_stage.webui[0].name
}

resource "aws_route53_record" "clhear" {
  count   = local.deploy_webui ? 1 : 0
  zone_id = data.aws_route53_zone.clhear[0].zone_id
  name    = var.clhear_hostname
  type    = "A"

  alias {
    name                   = aws_apigatewayv2_domain_name.webui[0].domain_name_configuration[0].target_domain_name
    zone_id                = aws_apigatewayv2_domain_name.webui[0].domain_name_configuration[0].hosted_zone_id
    evaluate_target_health = false
  }
}
