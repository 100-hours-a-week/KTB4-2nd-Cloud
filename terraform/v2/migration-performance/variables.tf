variable "images" {
  description = "Completed, reviewed no-reboot AMIs and /dev/sdf snapshots from the original rehearsal hosts. Never use a live instance's data volume ID."
  type = map(object({
    ami_id           = string
    data_snapshot_id = string
  }))
  validation {
    condition = (
      toset(keys(var.images)) == toset(["source", "target"]) &&
      alltrue([for image in values(var.images) : can(regex("^ami-[0-9a-f]{17}$", image.ami_id)) && can(regex("^snap-[0-9a-f]{17}$", image.data_snapshot_id))])
    )
    error_message = "Provide exactly source and target, each with a reviewed AMI and data snapshot ID."
  }
}
