terraform {
  backend "s3" {
    key          = "yeodam/v3/foundation/terraform.tfstate"
    region       = "ap-northeast-2"
    encrypt      = true
    use_lockfile = true
  }
}
