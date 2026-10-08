data "aws_caller_identity" "current" {}

module "app_storage" {
  source = "../modules/app-storage"

  bucket_name    = "yeodam-v2-staging-app-data-${data.aws_caller_identity.current.account_id}-${var.aws_region}"
  browser_origin = "https://staging.yeodam-2gether.com"
  tags           = local.common_tags
}

data "aws_iam_policy_document" "backend_task_assume_role" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "backend_task" {
  name               = "yeodam-v2-staging-backend-task"
  assume_role_policy = data.aws_iam_policy_document.backend_task_assume_role.json

  tags = local.common_tags
}

data "aws_iam_policy_document" "backend_photo_storage" {
  statement {
    sid       = "ReadBucketLocation"
    actions   = ["s3:GetBucketLocation"]
    resources = [module.app_storage.bucket_arn]
  }

  statement {
    sid = "ManageTripUploads"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:PutObjectTagging",
      "s3:DeleteObject",
    ]
    resources = ["${module.app_storage.bucket_arn}/trip-uploads/*"]
  }

  statement {
    sid = "ManageTemporaryDownloadArchives"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:PutObjectTagging",
    ]
    resources = ["${module.app_storage.bucket_arn}/trip-downloads/*"]
  }
}

resource "aws_iam_role_policy" "backend_photo_storage" {
  name   = "yeodam-v2-staging-backend-photo-storage"
  role   = aws_iam_role.backend_task.id
  policy = data.aws_iam_policy_document.backend_photo_storage.json
}
