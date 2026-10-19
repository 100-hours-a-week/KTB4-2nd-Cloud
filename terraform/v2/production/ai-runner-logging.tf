resource "aws_cloudwatch_log_group" "ai_runner" {
  name              = "/yeodam/v2/production/ai-runner"
  retention_in_days = 30
  tags              = { Role = "ai-runner-logs" }
}
resource "aws_iam_role_policy" "ai_runner_logs" {
  name = "production-ai-runner-cloudwatch"
  role = data.aws_iam_role.shared_gpu.name
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"], Resource = "${aws_cloudwatch_log_group.ai_runner.arn}:*"
  }] })
}
resource "aws_iam_role_policy" "plg_runner_logs" {
  name = "production-ai-runner-log-read"
  role = aws_iam_role.plg.name
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Action = ["logs:StartQuery", "logs:StopQuery", "logs:GetQueryResults", "logs:GetLogGroupFields", "logs:GetLogEvents"], Resource = aws_cloudwatch_log_group.ai_runner.arn
  }] })
}
resource "aws_ssm_document" "ai_runner_logs" {
  name          = "yeodam-v2-shared-ai-runner-logs-install"
  document_type = "Command"
  content = jsonencode({ schemaVersion = "2.2", description = "Collect fixed runner logs without application restart", mainSteps = [{
    action = "aws:runShellScript", name = "installRunnerLogs", inputs = { timeoutSeconds = "120", runCommand = concat([
      "set -eu", "install -d -m 0755 /usr/local/lib/yeodam-ai-runner-ops"
      ], [for name in ["logs.py", "level.py", "install.sh", "cloudwatch.json"] : "printf '%s' '${filebase64("${path.module}/../../../monitoring/ai-runner/${name}")}' | base64 --decode > /usr/local/lib/yeodam-ai-runner-ops/${name}"], [
      "bash /usr/local/lib/yeodam-ai-runner-ops/install.sh"
    ]) }
  }] })
}
resource "aws_ssm_document" "ai_runner_log_level" {
  name          = "yeodam-v2-shared-ai-runner-log-level"
  document_type = "Command"
  content = jsonencode({ schemaVersion = "2.2", description = "Inspect or change only an idle process runner log level", parameters = {
    Environment = { type = "String", allowedValues = ["staging", "production"] },
    Level       = { type = "String", default = "INFO", allowedValues = ["DEBUG", "INFO"] },
    Action      = { type = "String", default = "inspect", allowedValues = ["inspect", "set"] }
    }, mainSteps = [{ action = "aws:runShellScript", name = "runnerLogLevel", inputs = { timeoutSeconds = "180", runCommand = [
      "set -eu", "test -x /usr/local/sbin/yeodam-ai-runner-log-level",
      "if [ '{{ Action }}' = inspect ]; then sudo /usr/local/sbin/yeodam-ai-runner-log-level --environment '{{ Environment }}' --inspect; else sudo /usr/local/sbin/yeodam-ai-runner-log-level --environment '{{ Environment }}' --level '{{ Level }}'; fi"
  ] } }] })
}
