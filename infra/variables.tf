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
  description = "SSM param holding the Aurora DSN (schemas l0_platform … l8, community)"
}

# --- The record: Aurora Serverless v2 (HLD v2 I7) ------------------------
variable "aurora_enabled" {
  type        = bool
  default     = false
  description = "Deploy Aurora PostgreSQL Serverless v2 as the record. Needs existing_vpc_id/subnets."
}

variable "aurora_engine_version" {
  type    = string
  default = "16.6"
}

variable "aurora_max_acu" {
  type    = number
  default = 4
}

# --- Inference: Reg42 Infer on Bedrock (HLD v2 I6) ----------------------
variable "infer_base_url" {
  type    = string
  default = "https://infer.reg42.ai/v1"
}

# --- Datalake cross-region replication (DR) ----------------------------
variable "replication_enabled" {
  type        = bool
  default     = false
  description = "Replicate the Object-Locked datalake to replica_region (needs the aws.replica provider)."
}

variable "replica_region" {
  type    = string
  default = "eu-west-1"
}
