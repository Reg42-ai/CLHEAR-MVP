# Private subnets for the record tier (Aurora cutover, HLD v2 I7).
#
# The account's default VPC has only public subnets and no NAT. The web Lambda
# must sit inside the VPC to reach Aurora, and a Lambda in a VPC has no internet
# path of its own, so it needs a NAT gateway for SSM, S3, SES, Cognito and the
# private Infer router. The existing subnets are shared with the workforce
# tasks that rely on public IPs, so their route table is left alone: two new
# private subnets get their own route table through a new NAT gateway. Aurora
# lives in these subnets too. Created only with the record (aurora_enabled).

locals {
  deploy_private_net = local.deploy_aurora
  private_subnet_cidrs = {
    a = { cidr = "172.31.96.0/20", az = "${var.aws_region}a" }
    b = { cidr = "172.31.112.0/20", az = "${var.aws_region}b" }
  }
  private_subnet_ids = local.deploy_private_net ? [for k in sort(keys(local.private_subnet_cidrs)) : aws_subnet.private[k].id] : []
}

resource "aws_subnet" "private" {
  for_each          = local.deploy_private_net ? local.private_subnet_cidrs : {}
  vpc_id            = var.existing_vpc_id
  cidr_block        = each.value.cidr
  availability_zone = each.value.az

  map_public_ip_on_launch = false

  tags = { Name = "${var.name_prefix}-private-${each.key}" }
}

resource "aws_eip" "nat" {
  count  = local.deploy_private_net ? 1 : 0
  domain = "vpc"
  tags   = { Name = "${var.name_prefix}-nat" }
}

resource "aws_nat_gateway" "clhear" {
  count         = local.deploy_private_net ? 1 : 0
  allocation_id = aws_eip.nat[0].id
  subnet_id     = var.existing_private_subnet_ids[0] # a public subnet with an IGW route
  tags          = { Name = "${var.name_prefix}-nat" }
}

resource "aws_route_table" "private" {
  count  = local.deploy_private_net ? 1 : 0
  vpc_id = var.existing_vpc_id
  tags   = { Name = "${var.name_prefix}-private" }
}

resource "aws_route" "private_default" {
  count                  = local.deploy_private_net ? 1 : 0
  route_table_id         = aws_route_table.private[0].id
  destination_cidr_block = "0.0.0.0/0"
  nat_gateway_id         = aws_nat_gateway.clhear[0].id
}

resource "aws_route_table_association" "private" {
  for_each       = local.deploy_private_net ? local.private_subnet_cidrs : {}
  subnet_id      = aws_subnet.private[each.key].id
  route_table_id = aws_route_table.private[0].id
}

# The web tier's identity on the network: admitted by the Aurora and Infer SGs.
resource "aws_security_group" "webui" {
  count       = local.deploy_private_net && local.deploy_webui ? 1 : 0
  name        = "${var.name_prefix}-webui"
  description = "CLHEAR web Lambda inside the VPC"
  vpc_id      = var.existing_vpc_id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}
