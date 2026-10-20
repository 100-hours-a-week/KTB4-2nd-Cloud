resource "aws_cloudwatch_log_group" "ai_runner" {
  name              = "/yeodam/v2/staging/ai-runner"
  retention_in_days = 7
  tags              = merge(local.common_tags, { Role = "ai-runner-logs" })
}
resource "aws_iam_role_policy" "ai_runner_logs" {
  name = "staging-ai-runner-cloudwatch"
  role = aws_iam_role.ai_gpu.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"], Resource = "${aws_cloudwatch_log_group.ai_runner.arn}:*"
  }] })
}
resource "aws_iam_role_policy" "plg_runner_logs" {
  name = "staging-ai-runner-log-read"
  role = aws_iam_role.plg.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Action = ["logs:StartQuery", "logs:StopQuery", "logs:GetQueryResults", "logs:GetLogGroupFields", "logs:GetLogEvents"], Resource = aws_cloudwatch_log_group.ai_runner.arn
  }] })
}
