variable "aws_region" {
  type    = string
  default = "us-east-1"
}

variable "name_prefix" {
  type    = string
  default = "clhear"
}

variable "datalake_bucket_name" {
  type    = string
  default = "reg42-clhear-datalake"
}

# Object Lock (compliance mode) default retention. Cannot be retrofitted onto
# an existing bucket; compliance-mode objects are undeletable until expiry.
variable "object_lock_retention_days" {
  type    = number
  default = 30
}

# --- Existing reg42-infra resources. Leave empty to create a minimal
# --- self-contained stack (this repo cannot see reg42-infra).
variable "existing_ecs_cluster_arn" {
  type    = string
  default = ""
}

variable "existing_vpc_id" {
  type    = string
  default = ""
}

variable "existing_private_subnet_ids" {
  type    = list(string)
  default = []
}

# Host rule clhear.reg42.ai on the existing ALB; skipped when empty.
variable "existing_alb_listener_arn" {
  type    = string
  default = ""
}

variable "existing_web_target_group_arn" {
  type    = string
  default = ""
}

variable "clhear_hostname" {
  type    = string
  default = "clhear.reg42.ai"
}

# Same image as reg42-os (HLD: no new image); entrypoint python -m app.clhear.workers
variable "worker_image" {
  type    = string
  default = ""
}

variable "worker_max_count" {
  # Snapshot mode is single-writer (SQLite in S3): exactly one worker.
  type    = number
  default = 1
}

variable "worker_assign_public_ip" {
  # True when workers run in public subnets (default VPC) and need egress
  # without a NAT gateway (near-zero idle cost posture).
  type    = bool
  default = false
}

variable "schedules_enabled" {
  type    = bool
  default = true
}

variable "database_url_ssm_param" {
  type        = string
  default     = "/clhear/DATABASE_URL"
  description = "SSM param holding the Aurora DSN for schemas l0_platform/l1_sources"
}
