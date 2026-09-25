# CloudFront + AWS WAF in front of clhear.reg42.ai. An HTTP API cannot attach
# WAF directly, so the edge sits before the API Gateway custom domain.
# CloudFront adds a private origin header; the app rejects requests without it
# (CLHEAR_ORIGIN_VERIFY_SECRET), so the regional endpoint cannot be used to
# bypass the WAF. DNS moves to the distribution only after it is verified
# (scripts/edge_cutover.py); Terraform does not revert that record.

variable "edge_enabled" {
  type    = bool
  default = false
}

variable "edge_origin_domain" {
  description = "Regional target of the API Gateway custom domain (d-....execute-api.<region>.amazonaws.com)."
  type        = string
  default     = ""
}

variable "edge_rate_limit_per_5_minutes" {
  type    = number
  default = 2000
}

locals {
  deploy_edge = var.edge_enabled && var.edge_origin_domain != ""
}

data "aws_acm_certificate" "clhear_edge" {
  count       = local.deploy_edge ? 1 : 0
  domain      = var.clhear_hostname
  statuses    = ["ISSUED"]
  most_recent = true
}

resource "random_password" "edge_origin" {
  count   = local.deploy_edge ? 1 : 0
  length  = 48
  special = false
}

resource "aws_ssm_parameter" "edge_origin" {
  count       = local.deploy_edge ? 1 : 0
  name        = "/clhear/web/CLHEAR_ORIGIN_VERIFY_SECRET"
  description = "Header value CloudFront sends to the CLHEAR origin"
  type        = "SecureString"
  value       = random_password.edge_origin[0].result
}

resource "aws_wafv2_web_acl" "edge" {
  count       = local.deploy_edge ? 1 : 0
  name        = "${var.name_prefix}-edge"
  description = "CLHEAR edge: AWS managed rules and a per-IP rate limit"
  scope       = "CLOUDFRONT"

  default_action {
    allow {}
  }

  dynamic "rule" {
    for_each = {
      AWSManagedRulesCommonRuleSet          = 10
      AWSManagedRulesKnownBadInputsRuleSet  = 20
      AWSManagedRulesAmazonIpReputationList = 30
    }
    content {
      name     = rule.key
      priority = rule.value
      override_action {
        none {}
      }
      statement {
        managed_rule_group_statement {
          name        = rule.key
          vendor_name = "AWS"
          # Sign-in and API bodies legitimately exceed the common rule set's 8 KB body check.
          dynamic "rule_action_override" {
            for_each = rule.key == "AWSManagedRulesCommonRuleSet" ? ["SizeRestrictions_BODY"] : []
            content {
              name = rule_action_override.value
              action_to_use {
                count {}
              }
            }
          }
        }
      }
      visibility_config {
        cloudwatch_metrics_enabled = true
        metric_name                = "${var.name_prefix}-${rule.key}"
        sampled_requests_enabled   = true
      }
    }
  }

  rule {
    name     = "rate-per-ip"
    priority = 40
    action {
      block {
        custom_response {
          response_code = 429
          response_header {
            name  = "Retry-After"
            value = "60"
          }
        }
      }
    }
    statement {
      rate_based_statement {
        limit              = var.edge_rate_limit_per_5_minutes
        aggregate_key_type = "IP"
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${var.name_prefix}-rate-per-ip"
      sampled_requests_enabled   = true
    }
  }

  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = "${var.name_prefix}-edge"
    sampled_requests_enabled   = true
  }
}

resource "aws_cloudfront_distribution" "edge" {
  count           = local.deploy_edge ? 1 : 0
  enabled         = true
  comment         = "CLHEAR web app and /v1 behind AWS WAF"
  aliases         = [var.clhear_hostname]
  web_acl_id      = aws_wafv2_web_acl.edge[0].arn
  is_ipv6_enabled = true
  price_class     = "PriceClass_100"

  origin {
    origin_id   = "clhear-api"
    domain_name = var.edge_origin_domain
    custom_origin_config {
      http_port              = 80
      https_port             = 443
      origin_protocol_policy = "https-only"
      origin_ssl_protocols   = ["TLSv1.2"]
      origin_read_timeout    = 30
    }
    custom_header {
      name  = "X-CLHEAR-Origin"
      value = random_password.edge_origin[0].result
    }
  }

  # Pages and APIs: never cached at the edge; every viewer header, cookie and
  # query string reaches the origin (the Host header selects the custom domain).
  default_cache_behavior {
    target_origin_id         = "clhear-api"
    viewer_protocol_policy   = "redirect-to-https"
    allowed_methods          = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
    cached_methods           = ["GET", "HEAD"]
    cache_policy_id          = "4135ea2d-6df8-44a3-9df3-4b5a84be39ad" # Managed-CachingDisabled
    origin_request_policy_id = "216adef6-5c7f-47e4-b989-5492eafa07d3" # Managed-AllViewer
    compress                 = true
  }

  # Content-hashed static assets are immutable.
  ordered_cache_behavior {
    path_pattern             = "/static/*"
    target_origin_id         = "clhear-api"
    viewer_protocol_policy   = "redirect-to-https"
    allowed_methods          = ["GET", "HEAD"]
    cached_methods           = ["GET", "HEAD"]
    cache_policy_id          = "658327ea-f89d-4fab-a63d-7e88639e58f6" # Managed-CachingOptimized
    origin_request_policy_id = "216adef6-5c7f-47e4-b989-5492eafa07d3"
    compress                 = true
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    acm_certificate_arn      = data.aws_acm_certificate.clhear_edge[0].arn
    ssl_support_method       = "sni-only"
    minimum_protocol_version = "TLSv1.2_2021"
  }
}

output "edge_distribution_domain" {
  value = local.deploy_edge ? aws_cloudfront_distribution.edge[0].domain_name : null
}
