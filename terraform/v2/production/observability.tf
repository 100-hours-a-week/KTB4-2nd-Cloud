locals {
  observation_peers = {
    backend = { sg = aws_security_group.backend_tasks.id, ports = [9091] },
    ai      = { sg = aws_security_group.ai_internal_alb.id, ports = [80] },
    mysql   = { sg = module.mysql.security_group_id, ports = [9100, 9104, 9187] },
    redis   = { sg = aws_security_group.ai_redis.id, ports = [9100, 9121] },
    kafka   = { sg = aws_security_group.kafka.id, ports = [9100, 9308, 9404, 9187] },
    gpu     = { sg = data.aws_security_group.shared_gpu.id, ports = [9100, 12346] }
  }
  observation_edges = merge([for name, p in local.observation_peers : { for port in p.ports : "${name}-${port}" => { sg = p.sg, port = port } }]...)
  log_peers         = { mysql = module.mysql.security_group_id, redis = aws_security_group.ai_redis.id, kafka = aws_security_group.kafka.id, gpu = data.aws_security_group.shared_gpu.id }
}
resource "aws_vpc_security_group_egress_rule" "plg_observers" {
  for_each                     = local.observation_edges
  security_group_id            = aws_security_group.plg.id
  referenced_security_group_id = each.value.sg
  ip_protocol                  = "tcp"
  from_port                    = each.value.port
  to_port                      = each.value.port
  description                  = "Production PLG only to ${each.key}"
}
resource "aws_vpc_security_group_ingress_rule" "observers_plg" {
  for_each                     = local.observation_edges
  security_group_id            = each.value.sg
  referenced_security_group_id = aws_security_group.plg.id
  ip_protocol                  = "tcp"
  from_port                    = each.value.port
  to_port                      = each.value.port
  description                  = "Private production observer only from PLG"
}
resource "aws_vpc_security_group_ingress_rule" "plg_host_logs" {
  for_each                     = local.log_peers
  security_group_id            = aws_security_group.plg.id
  referenced_security_group_id = each.value
  ip_protocol                  = "tcp"
  from_port                    = 3100
  to_port                      = 3100
  description                  = "Fixed production host log shipper"
}
resource "aws_vpc_security_group_egress_rule" "host_logs_plg" {
  for_each                     = local.log_peers
  security_group_id            = each.value
  referenced_security_group_id = aws_security_group.plg.id
  ip_protocol                  = "tcp"
  from_port                    = 3100
  to_port                      = 3100
  description                  = "Production host logs only to private Loki"
}
resource "aws_vpc_security_group_ingress_rule" "plg_gpu_metrics" {
  security_group_id            = aws_security_group.plg.id
  referenced_security_group_id = data.aws_security_group.shared_gpu.id
  ip_protocol                  = "tcp"
  from_port                    = 9090
  to_port                      = 9090
  description                  = "Production GPU Alloy metrics only"
}
resource "aws_vpc_security_group_egress_rule" "gpu_metrics_plg" {
  security_group_id            = data.aws_security_group.shared_gpu.id
  referenced_security_group_id = aws_security_group.plg.id
  ip_protocol                  = "tcp"
  from_port                    = 9090
  to_port                      = 9090
  description                  = "Production GPU Alloy remote-write only"
}
