# Event plane (HLD v2 §3).
#   * `clhear` custom bus: every layer publishes clhear.<layer>.<event>
#     (derived | changed | invalidated | below_gate) via the outbox relay.
#   * Fan-out rules route a layer's events to the queues of the fleets that
#     derive from it (strictly downward, I1). below_gate goes to L0.
#   * Archive keeps 365 days of events for replay (I3 reproducibility).
#   * Schedules enqueue jobs directly on the owning fleet's queue.

resource "aws_cloudwatch_event_bus" "clhear" {
  name = var.name_prefix
}

resource "aws_cloudwatch_event_archive" "clhear" {
  name             = "${var.name_prefix}-events"
  event_source_arn = aws_cloudwatch_event_bus.clhear.arn
  retention_days   = 365
  event_pattern = jsonencode({
    source = ["clhear"]
  })
}

locals {
  # producer layer -> consumer fleets (who reads whom, always downward).
  fanout = {
    l1 = ["l2", "l7"]
    l2 = ["l3", "l4", "l5", "l7"]
    l3 = ["l4", "l5", "l6"]
    l4 = ["l6"]
    l5 = ["l6"]
    l6 = ["l8"]
    l7 = ["l6"]
    l8 = []
  }
  fanout_pairs = merge([
    for src, dsts in local.fanout : { for dst in dsts : "${src}-${dst}" => { src = src, dst = dst } }
  ]...)
}

resource "aws_cloudwatch_event_rule" "layer_events" {
  for_each       = local.fanout
  name           = "${var.name_prefix}-${each.key}-events"
  event_bus_name = aws_cloudwatch_event_bus.clhear.name
  event_pattern = jsonencode({
    source        = ["clhear"]
    "detail-type" = [{ prefix = "clhear.${each.key}." }]
  })
}

resource "aws_cloudwatch_event_target" "layer_fanout" {
  for_each       = local.fanout_pairs
  rule           = aws_cloudwatch_event_rule.layer_events[each.value.src].name
  event_bus_name = aws_cloudwatch_event_bus.clhear.name
  target_id      = "fleet-${each.value.dst}"
  arn            = local.fleet_queue[each.value.dst].arn
  input_path     = "$.detail" # the envelope itself; workers already speak it
  dead_letter_config {
    arn = aws_sqs_queue.events_dlq.arn
  }
}

# Any layer dropping below its gate is platform business (freeze publication).
resource "aws_cloudwatch_event_rule" "below_gate" {
  name           = "${var.name_prefix}-below-gate"
  event_bus_name = aws_cloudwatch_event_bus.clhear.name
  event_pattern = jsonencode({
    source        = ["clhear"]
    "detail-type" = [{ suffix = ".below_gate" }]
  })
}

resource "aws_cloudwatch_event_target" "below_gate_to_l0" {
  rule           = aws_cloudwatch_event_rule.below_gate.name
  event_bus_name = aws_cloudwatch_event_bus.clhear.name
  target_id      = "fleet-l0"
  arn            = local.fleet_queue["l0"].arn
  input_path     = "$.detail"
}

# --- Schedules -------------------------------------------------------------
# One rule per adapter schedule. Cron times (UTC) mirror FLEET_SCHEDULES in
# app/clhear/l1/models.py — the UI shows that dictionary, so keep the two in sync.
locals {
  adapter_schedules = {
    uk_legislation    = "cron(0 0 * * ? *)"
    eur_lex           = "cron(0 0 * * ? *)"
    govinfo_us        = "cron(0 0 * * ? *)"
    fca_handbook      = "cron(0 0 * * ? *)"
    sec_edgar         = "cron(0 0 * * ? *)"
    fca_enforcement   = "cron(0 0 * * ? *)"
    sec_enforcement   = "cron(0 0 * * ? *)"
    finra_enforcement = "cron(0 0 * * ? *)"
    esma              = "cron(0 0 * * ? *)"
    bis_basel         = "cron(0 0 * * ? *)"
    iosco             = "cron(0 0 * * ? *)"
    asic              = "cron(0 0 * * ? *)"
    isa               = "cron(0 0 * * ? *)"
    au_legislation    = "cron(0 0 * * ? *)"
    sg_legislation    = "cron(0 0 * * ? *)"
    finra             = "cron(0 0 * * ? *)"
    adgm              = "cron(0 0 * * ? *)"
    nydfs             = "cron(0 0 * * ? *)"
    nasdaq            = "cron(0 0 * * ? *)"
    malta             = "cron(0 0 * * ? *)"
    uae               = "cron(0 0 * * ? *)"
    cysec             = "cron(0 0 * * ? *)"
    mas               = "cron(0 0 * * ? *)"
    fatf              = "cron(0 0 * * ? *)"
    wolfsberg         = "cron(0 0 * * ? *)"
    irs_gov           = "cron(0 0 * * ? *)"
    lists             = "cron(0 0 * * ? *)"
    overlay           = "cron(0 0 * * ? *)"
    restricted_file   = "cron(0 0 * * ? *)"
    seychelles        = "cron(0 0 * * ? *)"
    gibraltar         = "cron(0 0 * * ? *)"
    israel            = "cron(0 0 * * ? *)"
  }
}

resource "aws_cloudwatch_event_rule" "adapter" {
  for_each            = local.adapter_schedules
  name                = "${var.name_prefix}-adapter-${each.key}"
  schedule_expression = each.value
  state               = var.schedules_enabled ? "ENABLED" : "DISABLED"
}

resource "aws_cloudwatch_event_target" "adapter_to_sqs" {
  for_each = local.adapter_schedules
  rule     = aws_cloudwatch_event_rule.adapter[each.key].name
  arn      = local.fleet_queue["l1"].arn
  input_transformer {
    input_paths    = { event_id = "$.id", event_time = "$.time" }
    input_template = <<-JSON
{"event_id":<event_id>,"layer":"l1","kind":"AdapterRunRequested","subject_ref":"${each.key}","payload":{"adapter":"${each.key}"},"schema_version":1,"producer":"eventbridge","ts":<event_time>}
    JSON
  }
}

# Nightly projection rebuild (HLD v2 I7): Neo4j graph + pgvector index are
# rebuilt from Postgres before the release is cut. Idempotent — rerunning it
# on an unchanged record changes nothing but the run log.
resource "aws_cloudwatch_event_rule" "graph_rebuild" {
  name                = "${var.name_prefix}-graph-rebuild"
  schedule_expression = "cron(0 23 * * ? *)"
  state               = var.schedules_enabled ? "ENABLED" : "DISABLED"
}

resource "aws_cloudwatch_event_target" "graph_rebuild_to_sqs" {
  rule = aws_cloudwatch_event_rule.graph_rebuild.name
  arn  = local.fleet_queue["l0"].arn
  input_transformer {
    input_paths    = { event_id = "$.id", event_time = "$.time" }
    input_template = <<-JSON
{"event_id":<event_id>,"layer":"l0","kind":"GraphRebuildRequested","subject_ref":"all","payload":{"index":true,"force":false},"schema_version":1,"producer":"eventbridge","ts":<event_time>}
    JSON
  }
}

# Nightly DR drill (HLD v2 §7.1, item 17): the L0 fleet pg_dumps the record,
# pg_restores it into the scratch database on the same cluster, rebuilds the
# graph projection into the Neo4j `drill` database and samples the cross-region
# datalake replica. RPO/RTO land in l0_platform.dr_drills; a failed drill
# publishes DrDrillPassed=0 (alarm in observability.tf).
resource "aws_cloudwatch_event_rule" "dr_drill" {
  name                = "${var.name_prefix}-dr-drill"
  schedule_expression = "cron(0 4 * * ? *)" # after the 01:15 release and the 03:00 Aurora backup window
  state               = var.schedules_enabled ? "ENABLED" : "DISABLED"
}

resource "aws_cloudwatch_event_target" "dr_drill_to_sqs" {
  rule = aws_cloudwatch_event_rule.dr_drill.name
  arn  = local.fleet_queue["l0"].arn
  input_transformer {
    input_paths    = { event_id = "$.id", event_time = "$.time" }
    input_template = <<-JSON
{"event_id":<event_id>,"layer":"l0","kind":"DrDrillRequested","subject_ref":"all","payload":{"neo4j_database":"drill"},"schema_version":1,"producer":"eventbridge","ts":<event_time>}
    JSON
  }
}

# Nightly named release (semantic date). The L0 fleet snapshots the record,
# gates each layer on its evals and writes a pin-able manifest.
resource "aws_cloudwatch_event_rule" "eod_publish" {
  name                = "${var.name_prefix}-eod-publish"
  schedule_expression = "cron(30 23 * * ? *)"
  state               = var.schedules_enabled ? "ENABLED" : "DISABLED"
}

resource "aws_cloudwatch_event_target" "eod_publish_to_sqs" {
  rule = aws_cloudwatch_event_rule.eod_publish.name
  arn  = local.fleet_queue["l0"].arn
  input_transformer {
    input_paths    = { event_id = "$.id", event_time = "$.time" }
    input_template = <<-JSON
{"event_id":<event_id>,"layer":"l0","kind":"PublishReleaseRequested","subject_ref":"all","payload":{"layers":"gated"},"schema_version":1,"producer":"eventbridge","ts":<event_time>}
    JSON
  }
}
