variable "name_prefix" {
  type = string
}

variable "vpc_id" {
  type = string
}

variable "private_subnet_id" {
  type = string
}

variable "backend_security_group_id" {
  type = string
}

variable "node_type" {
  type = string
}

variable "tags" {
  type = map(string)
}
