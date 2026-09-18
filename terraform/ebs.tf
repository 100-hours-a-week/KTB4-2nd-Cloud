resource "aws_ebs_volume" "mysql_data" {
  availability_zone = aws_subnet.public.availability_zone

  type       = "gp3"
  size       = var.mysql_data_volume_size
  iops       = 3000
  throughput = 125
  encrypted  = true

  tags = {
    Name = "${local.name_prefix}-mysql-data"
    Role = "mysql"
  }

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_volume_attachment" "mysql_data" {
  device_name = "/dev/sdf"
  volume_id   = aws_ebs_volume.mysql_data.id
  instance_id = aws_instance.app.id

  stop_instance_before_detaching = true
}
