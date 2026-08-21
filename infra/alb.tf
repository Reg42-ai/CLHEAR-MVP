# Host rule clhear.reg42.ai -> existing web service target group.
# Skipped unless the existing listener + target group ARNs are provided
# (Route53/ACM for reg42.ai already live in reg42-infra).
resource "aws_lb_listener_rule" "clhear_host" {
  count        = var.existing_alb_listener_arn != "" && var.existing_web_target_group_arn != "" ? 1 : 0
  listener_arn = var.existing_alb_listener_arn
  priority     = 42

  action {
    type             = "forward"
    target_group_arn = var.existing_web_target_group_arn
  }

  condition {
    host_header {
      values = [var.clhear_hostname]
    }
  }
}
