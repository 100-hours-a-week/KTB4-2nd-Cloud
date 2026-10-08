output "instance_id" { value = aws_instance.mysql.id }
output "private_ip" { value = aws_instance.mysql.private_ip }
output "data_volume_id" { value = aws_ebs_volume.data.id }
output "root_password_secret_arn" { value = aws_secretsmanager_secret.root_password.arn }
output "security_group_id" { value = aws_security_group.mysql.id }
