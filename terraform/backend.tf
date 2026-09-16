terraform {
  backend "s3" {
    key          = "yeodam/v1/terraform.tfstate"
    region       = "ap-northeast-2"
    encrypt      = true
    use_lockfile = true
  }
}
