# Existing addresses and AMI inputs stay stable for live PLG, generator,
# Kafka, AI and validation hosts. Their names predate the rehearsal retirement.

data "aws_ami" "v1_source_ubuntu" {
  owners      = ["099720109477"]
  most_recent = false

  filter {
    name   = "image-id"
    values = [var.v1_source_ami_id]
  }

  filter {
    name   = "architecture"
    values = ["x86_64"]
  }

  filter {
    name   = "root-device-type"
    values = ["ebs"]
  }
}

data "aws_iam_policy_document" "v1_source_ec2_assume_role" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}
