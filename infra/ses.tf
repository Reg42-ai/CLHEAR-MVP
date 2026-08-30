# SES for contributor magic-link emails (noreply@clhear.reg42.ai).
# Domain identity + DKIM verified via the existing reg42.ai Route53 zone.
# NOTE: new SES accounts start in sandbox (verified recipients only) —
# request production access in the SES console to reach any address.
resource "aws_sesv2_email_identity" "clhear" {
  count          = var.clhear_hostname != "" ? 1 : 0
  email_identity = var.clhear_hostname
}

resource "aws_route53_record" "ses_dkim" {
  count   = var.clhear_hostname != "" ? 3 : 0
  zone_id = data.aws_route53_zone.clhear[0].zone_id
  name    = "${aws_sesv2_email_identity.clhear[0].dkim_signing_attributes[0].tokens[count.index]}._domainkey.${var.clhear_hostname}"
  type    = "CNAME"
  ttl     = 600
  records = ["${aws_sesv2_email_identity.clhear[0].dkim_signing_attributes[0].tokens[count.index]}.dkim.amazonses.com"]
}

resource "aws_ssm_parameter" "session_secret" {
  name  = "/clhear/SESSION_SECRET"
  type  = "SecureString"
  value = "CHANGEME"
  lifecycle {
    ignore_changes = [value]
  }
}

resource "aws_ssm_parameter" "google_oauth_client_id" {
  name  = "/clhear/GOOGLE_OAUTH_CLIENT_ID"
  type  = "SecureString"
  value = "CHANGEME"
  lifecycle {
    ignore_changes = [value]
  }
}

resource "aws_ssm_parameter" "google_oauth_client_secret" {
  name  = "/clhear/GOOGLE_OAUTH_CLIENT_SECRET"
  type  = "SecureString"
  value = "CHANGEME"
  lifecycle {
    ignore_changes = [value]
  }
}
