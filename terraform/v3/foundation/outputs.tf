output "account_id" {
  value = data.aws_caller_identity.current.account_id
}

output "cluster_name" {
  value = aws_eks_cluster.this.name
}

output "cluster_arn" {
  value = aws_eks_cluster.this.arn
}

output "vpc_id" {
  value = aws_vpc.this.id
}

output "private_subnet_ids" {
  value = { for az, subnet in aws_subnet.private : az => subnet.id }
}

output "node_groups" {
  value = { for az, group in aws_eks_node_group.system : az => group.node_group_name }
}

output "addon_versions" {
  value = var.addon_versions
}
