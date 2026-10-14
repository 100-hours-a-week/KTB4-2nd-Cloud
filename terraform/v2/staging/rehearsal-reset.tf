data "aws_iam_policy_document" "rehearsal_reset_snapshot" {
  statement {
    actions = ["s3:GetObject", "s3:PutObject", "s3:AbortMultipartUpload"]
    resources = [
      "${aws_s3_bucket.mysql_backup.arn}/migration/rehearsal-reset/*",
    ]
  }
}

resource "aws_iam_role_policy" "rehearsal_reset_snapshot" {
  name   = "yeodam-v2-staging-v1-source-rehearsal-reset"
  role   = aws_iam_role.v1_source_app.id
  policy = data.aws_iam_policy_document.rehearsal_reset_snapshot.json
}
