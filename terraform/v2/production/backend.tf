terraform {
  backend "s3" {
    key          = "yeodam/v2/production/terraform.tfstate"
    region       = "ap-northeast-2"
    encrypt      = true
    use_lockfile = true
  }
}
