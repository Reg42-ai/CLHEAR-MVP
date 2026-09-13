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

# --- The query graph: Neo4j Community on Fargate + EFS (HLD v2 I7) -------
variable "neo4j_enabled" {
  type        = bool
  default     = false
  description = "Deploy the Neo4j Community projection (rebuilt nightly from Postgres). Needs existing_vpc_id/subnets."
}

variable "neo4j_image" {
  type    = string
  default = "neo4j:5-community"
}

variable "neo4j_cpu" {
  type    = number
  default = 1024
}

variable "neo4j_memory" {
  type        = number
  default     = 4096
  description = "Task memory in MiB; heap and page cache each take a quarter"
}

# --- The community forum: Discourse on Fargate (HLD v2 §6) --------------
variable "discourse_enabled" {
  type        = bool
  default     = false
  description = "Deploy the Discourse forum (needs aurora_enabled, existing_vpc_id/subnets and the ALB listener)."
}

variable "discourse_hostname" {
  type    = string
  default = "community.clhear.reg42.ai"
}

variable "discourse_image" {
  type    = string
  default = "bitnami/discourse:3"
}

variable "discourse_redis_image" {
  type    = string
  default = "bitnami/redis:7.2"
}

variable "discourse_cpu" {
  type    = number
  default = 2048
}

variable "discourse_memory" {
  type        = number
  default     = 6144
  description = "Task memory in MiB shared by the web, Sidekiq and Redis containers"
}

variable "existing_alb_cidr_blocks" {
  type        = list(string)
  default     = ["10.0.0.0/8"]
  description = "CIDRs the existing ALB sends traffic from (the VPC range); restricts the forum's ingress"
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
