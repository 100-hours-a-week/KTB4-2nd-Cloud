terraform {
  backend "s3" {
    key          = "yeodam/v2/staging/terraform.tfstate"
    region       = "ap-northeast-2"
    encrypt      = true
    use_lockfile = true
  }
}
