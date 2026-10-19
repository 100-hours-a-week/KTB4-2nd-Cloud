# Extend the existing VPC. V1's subnet, routes, IGW and S3 endpoint stay in V1 state.
resource "aws_subnet" "public_secondary" {
  vpc_id                  = data.aws_vpc.production.id
  cidr_block              = "10.20.2.0/24"
  availability_zone       = "ap-northeast-2c"
  map_public_ip_on_launch = false

  tags = { Name = "${local.name_prefix}-public-secondary", Tier = "public" }
}

resource "aws_route_table" "public_secondary" {
  vpc_id = data.aws_vpc.production.id
  tags   = { Name = "${local.name_prefix}-public-secondary-rt", Tier = "public" }
}

resource "aws_route" "public_secondary_internet" {
  route_table_id         = aws_route_table.public_secondary.id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = data.aws_internet_gateway.production.id
}

resource "aws_route_table_association" "public_secondary" {
  subnet_id      = aws_subnet.public_secondary.id
  route_table_id = aws_route_table.public_secondary.id
}

resource "aws_subnet" "private_app" {
  vpc_id                  = data.aws_vpc.production.id
  cidr_block              = "10.20.10.0/24"
  availability_zone       = "ap-northeast-2a"
  map_public_ip_on_launch = false

  tags = { Name = "${local.name_prefix}-private-app", Tier = "private" }
}

resource "aws_eip" "nat" {
  domain = "vpc"
  tags   = { Name = "${local.name_prefix}-nat-eip" }
}

resource "aws_nat_gateway" "app" {
  allocation_id = aws_eip.nat.id
  subnet_id     = data.aws_subnet.v1_public.id
  tags          = { Name = "${local.name_prefix}-nat" }
}

resource "aws_route_table" "private_app" {
  vpc_id = data.aws_vpc.production.id
  tags   = { Name = "${local.name_prefix}-private-app-rt", Tier = "private" }
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

# A separate endpoint owns only the new route table; do not edit V1 endpoint associations.
resource "aws_vpc_endpoint" "s3" {
  vpc_id            = data.aws_vpc.production.id
  service_name      = "com.amazonaws.ap-northeast-2.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.private_app.id]
  tags              = { Name = "${local.name_prefix}-s3-endpoint" }
}

# Internal AI ALB requires a second AZ. Tasks stay in private_app (2a).
resource "aws_subnet" "private_ai_alb_secondary" {
  vpc_id                  = data.aws_vpc.production.id
  cidr_block              = "10.20.11.0/24"
  availability_zone       = "ap-northeast-2c"
  map_public_ip_on_launch = false
  tags                    = { Name = "${local.name_prefix}-private-ai-alb-secondary", Tier = "private" }
}

resource "aws_route_table_association" "private_ai_alb_secondary" {
  subnet_id      = aws_subnet.private_ai_alb_secondary.id
  route_table_id = aws_route_table.private_app.id
}
