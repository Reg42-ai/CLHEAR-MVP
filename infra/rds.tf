# The record (HLD v2 I7): Aurora PostgreSQL Serverless v2 with pgvector,
# scale-to-zero when idle. One schema per layer (l0_platform … l8_fills,
# community); the numbered migrations create them at startup.
# Enabled with aurora_enabled = true once the VPC is known.

locals {
  deploy_aurora = var.aurora_enabled && local.have_network
  # The cluster can exist (and be loaded) while fleets and web still run on the
  # S3 snapshot; record_cutover is the moment they switch. Kept separate so the
  # load happens into an idle database and the switch is one reviewed flip.
  record_on_aurora = local.deploy_aurora && var.record_cutover
}

variable "record_cutover" {
  type        = bool
  default     = false
  description = "Point fleets, web tier and clhear-infer at Aurora. Flip only after scripts/load_record has filled it."
}

resource "random_password" "aurora_master" {
  count   = local.deploy_aurora ? 1 : 0
  length  = 32
  special = false
}

resource "aws_db_subnet_group" "aurora" {
  count      = local.deploy_aurora ? 1 : 0
  name       = "${var.name_prefix}-aurora"
  subnet_ids = local.private_subnet_ids # network.tf: no public IPs, NAT egress
}

resource "aws_security_group" "aurora" {
  count       = local.deploy_aurora ? 1 : 0
  name        = "${var.name_prefix}-aurora"
  vpc_id      = var.existing_vpc_id
  description = "CLHEAR record - Postgres from the layer fleets and the web app only"

  ingress {
    description     = "Postgres from fleets, the web tier and the private Infer router (spend ledger)"
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = concat(aws_security_group.workers[*].id, aws_security_group.webui[*].id, aws_security_group.infer[*].id)
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_rds_cluster_parameter_group" "aurora" {
  count  = local.deploy_aurora ? 1 : 0
  name   = "${var.name_prefix}-aurora-pg16"
  family = "aurora-postgresql16"

  parameter {
    name         = "shared_preload_libraries"
    value        = "pg_stat_statements"
    apply_method = "pending-reboot" # static parameter; "immediate" is a perpetual plan diff
  }
  parameter {
    name  = "log_min_duration_statement"
    value = "1000"
  }
}

resource "aws_rds_cluster" "clhear" {
  count                           = local.deploy_aurora ? 1 : 0
  cluster_identifier              = "${var.name_prefix}-record"
  engine                          = "aurora-postgresql"
  engine_mode                     = "provisioned"
  engine_version                  = var.aurora_engine_version
  database_name                   = "clhear"
  master_username                 = "clhear"
  master_password                 = random_password.aurora_master[0].result
  db_subnet_group_name            = aws_db_subnet_group.aurora[0].name
  vpc_security_group_ids          = [aws_security_group.aurora[0].id]
  db_cluster_parameter_group_name = aws_rds_cluster_parameter_group.aurora[0].name
  storage_encrypted               = true
  backup_retention_period         = 35
  preferred_backup_window         = "02:00-03:00"
  copy_tags_to_snapshot           = true
  deletion_protection             = true
  skip_final_snapshot             = false
  final_snapshot_identifier       = "${var.name_prefix}-record-final"
  enabled_cloudwatch_logs_exports = ["postgresql"]

  serverlessv2_scaling_configuration {
    min_capacity             = 0 # scale to zero when idle (near-zero idle posture)
    max_capacity             = var.aurora_max_acu
    seconds_until_auto_pause = 1800
  }

  lifecycle {
    ignore_changes = [master_password]
  }
}

resource "aws_rds_cluster_instance" "clhear" {
  count                = local.deploy_aurora ? 1 : 0
  identifier           = "${var.name_prefix}-record-1"
  cluster_identifier   = aws_rds_cluster.clhear[0].id
  instance_class       = "db.serverless"
  engine               = aws_rds_cluster.clhear[0].engine
  engine_version       = aws_rds_cluster.clhear[0].engine_version
  db_subnet_group_name = aws_db_subnet_group.aurora[0].name
  publicly_accessible  = false
}

locals {
  aurora_dsn = local.deploy_aurora ? "postgresql+psycopg://clhear:${random_password.aurora_master[0].result}@${aws_rds_cluster.clhear[0].endpoint}:5432/clhear?sslmode=require" : ""
}

# Always the live DSN (unlike /clhear/DATABASE_URL, which stays CHANGEME until the
# cutover) so the one-off load job can reach the cluster before anything else does.
resource "aws_ssm_parameter" "aurora_dsn" {
  count       = local.deploy_aurora ? 1 : 0
  name        = "/clhear/AURORA_DSN"
  type        = "SecureString"
  value       = local.aurora_dsn
  description = "Aurora master DSN for the record; used by scripts/load_record.py before the cutover"
}

output "aurora_endpoint" {
  value = try(aws_rds_cluster.clhear[0].endpoint, "")
}
