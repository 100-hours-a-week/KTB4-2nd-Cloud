output "clone_hosts" {
  description = "New quarantined clone identities; not original staging resources"
  value = { for role, host in aws_instance.clone : role => {
    instance_id = host.id
    private_ip  = host.private_ip
    public_ip   = host.public_ip
    root_volume = host.root_block_device[0].volume_id
  } }
}

output "clone_data_volumes" {
  description = "Dedicated retained data volumes on the clone instances"
  value       = { for role, host in aws_instance.clone : role => [for device in host.ebs_block_device : device.volume_id if device.device_name == "/dev/sdf"][0] }
}
