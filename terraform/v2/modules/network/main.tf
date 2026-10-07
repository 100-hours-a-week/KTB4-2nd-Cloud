locals {
  public_subnets = {
    app = {
      availability_zone = var.app_availability_zone
      cidr_block        = cidrsubnet(var.vpc_cidr, 8, 0)
    }
    secondary = {
      availability_zone = var.alb_secondary_availability_zone
      cidr_block        = cidrsubnet(var.vpc_cidr, 8, 1)
    }
  }

  private_app_cidr = cidrsubnet(var.vpc_cidr, 8, 10)
}

resource "aws_vpc" "this" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-vpc"
  })
}

resource "aws_subnet" "public" {
  for_each = local.public_subnets

  vpc_id                  = aws_vpc.this.id
  availability_zone       = each.value.availability_zone
  cidr_block              = each.value.cidr_block
  map_public_ip_on_launch = false

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-public-${each.key}"
    Tier = "public"
  })
}

resource "aws_subnet" "private_app" {
  vpc_id                  = aws_vpc.this.id
  availability_zone       = var.app_availability_zone
  cidr_block              = local.private_app_cidr
  map_public_ip_on_launch = false

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-private-app"
    Tier = "private"
  })
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-igw"
  })
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-public-rt"
    Tier = "public"
  })
}

resource "aws_route" "public_internet" {
  route_table_id         = aws_route_table.public.id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.this.id
}

resource "aws_route_table_association" "public" {
  for_each = aws_subnet.public

  subnet_id      = each.value.id
  route_table_id = aws_route_table.public.id
}

resource "aws_eip" "nat" {
  domain = "vpc"

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-nat-eip"
  })
}

resource "aws_nat_gateway" "app" {
  allocation_id = aws_eip.nat.id
  subnet_id     = aws_subnet.public["app"].id

  depends_on = [aws_internet_gateway.this]

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-nat"
  })
}

resource "aws_route_table" "private_app" {
  vpc_id = aws_vpc.this.id

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-private-app-rt"
    Tier = "private"
  })
}

resource "aws_route" "private_internet" {
  route_table_id         = aws_route_table.private_app.id
  destination_cidr_block = "0.0.0.0/0"
  nat_gateway_id         = aws_nat_gateway.app.id
}

resource "aws_route_table_association" "private_app" {
  subnet_id      = aws_subnet.private_app.id
  route_table_id = aws_route_table.private_app.id
}

resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.this.id
  service_name      = "com.amazonaws.${var.aws_region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.private_app.id]

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-s3-endpoint"
  })
}
