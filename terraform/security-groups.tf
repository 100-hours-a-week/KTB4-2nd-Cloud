resource "aws_security_group" "app" {
  name        = "${local.name_prefix}-app-sg"
  description = "Security group for the Yeodam V1 App EC2 instance"
  vpc_id      = aws_vpc.main.id

  revoke_rules_on_delete = true

  tags = {
    Name = "${local.name_prefix}-app-sg"
    Role = "app"
  }
}

resource "aws_security_group" "worker" {
  name        = "${local.name_prefix}-worker-sg"
  description = "Security group for the Yeodam V1 Worker EC2 instance"
  vpc_id      = aws_vpc.main.id

  revoke_rules_on_delete = true

  tags = {
    Name = "${local.name_prefix}-worker-sg"
    Role = "worker"
  }
}

resource "aws_vpc_security_group_ingress_rule" "app_http" {
  security_group_id = aws_security_group.app.id

  description = "Allow public HTTP traffic to host Nginx"
  ip_protocol = "tcp"
  from_port   = 80
  to_port     = 80
  cidr_ipv4   = "0.0.0.0/0"
}

resource "aws_vpc_security_group_ingress_rule" "app_https" {
  security_group_id = aws_security_group.app.id

  description = "Allow public HTTPS traffic to host Nginx"
  ip_protocol = "tcp"
  from_port   = 443
  to_port     = 443
  cidr_ipv4   = "0.0.0.0/0"
}

resource "aws_vpc_security_group_ingress_rule" "worker_ai_from_app" {
  security_group_id = aws_security_group.worker.id

  description                  = "Allow AI API traffic only from the App security group"
  ip_protocol                  = "tcp"
  from_port                    = 8000
  to_port                      = 8000
  referenced_security_group_id = aws_security_group.app.id
}

resource "aws_vpc_security_group_egress_rule" "app_all_ipv4" {
  security_group_id = aws_security_group.app.id

  description = "Allow App outbound IPv4 traffic"
  ip_protocol = "-1"
  cidr_ipv4   = "0.0.0.0/0"
}

resource "aws_vpc_security_group_egress_rule" "worker_all_ipv4" {
  security_group_id = aws_security_group.worker.id

  description = "Allow Worker outbound IPv4 traffic"
  ip_protocol = "-1"
  cidr_ipv4   = "0.0.0.0/0"
}
