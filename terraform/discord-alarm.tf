data "archive_file" "discord_alarm_forwarder" {
  type        = "zip"
  source_file = "${path.module}/../lambda/discord_alarm_forwarder.py"
  output_path = "${path.module}/.terraform/${local.name_prefix}-discord-alarm-forwarder.zip"
}

data "aws_iam_policy_document" "discord_alarm_forwarder_assume_role" {
  statement {
    sid     = "AllowLambdaAssumeRole"
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "discord_alarm_forwarder" {
  name               = "${local.name_prefix}-discord-alarm-forwarder-role"
  description        = "Forwards Yeodam V1 CloudWatch alarm notifications to Discord"
  assume_role_policy = data.aws_iam_policy_document.discord_alarm_forwarder_assume_role.json

  tags = {
    Name = "${local.name_prefix}-discord-alarm-forwarder-role"
    Role = "observability"
  }
}

data "aws_iam_policy_document" "discord_alarm_forwarder" {
  statement {
    sid    = "ReadDiscordWebhookParameter"
    effect = "Allow"

    actions = [
      "ssm:GetParameter",
    ]

    resources = [
      "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter${local.discord_webhook_parameter_name}",
    ]
  }

  statement {
    sid    = "WriteFunctionLogs"
    effect = "Allow"

    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]

    resources = [
      "${aws_cloudwatch_log_group.discord_alarm_forwarder.arn}:*",
    ]
  }
}

resource "aws_iam_role_policy" "discord_alarm_forwarder" {
  name   = "${local.name_prefix}-discord-alarm-forwarder"
  role   = aws_iam_role.discord_alarm_forwarder.id
  policy = data.aws_iam_policy_document.discord_alarm_forwarder.json
}

resource "aws_cloudwatch_log_group" "discord_alarm_forwarder" {
  name              = "/aws/lambda/${local.name_prefix}-discord-alarm-forwarder"
  retention_in_days = 30

  tags = {
    Name = "${local.name_prefix}-discord-alarm-forwarder"
    Role = "observability"
  }
}

resource "aws_lambda_function" "discord_alarm_forwarder" {
  function_name = "${local.name_prefix}-discord-alarm-forwarder"
  description   = "Forwards Yeodam V1 CloudWatch alarm state changes to Discord"
  role          = aws_iam_role.discord_alarm_forwarder.arn
  handler       = "discord_alarm_forwarder.lambda_handler"
  runtime       = "python3.12"
  architectures = ["arm64"]
  memory_size   = 128
  timeout       = 10

  filename         = data.archive_file.discord_alarm_forwarder.output_path
  source_code_hash = data.archive_file.discord_alarm_forwarder.output_base64sha256

  environment {
    variables = {
      DISCORD_WEBHOOK_PARAMETER_NAME = local.discord_webhook_parameter_name
    }
  }

  depends_on = [aws_iam_role_policy.discord_alarm_forwarder]

  tags = {
    Name = "${local.name_prefix}-discord-alarm-forwarder"
    Role = "observability"
  }
}

resource "aws_lambda_permission" "allow_operations_alarm_sns" {
  statement_id  = "AllowOperationsAlarmSNS"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.discord_alarm_forwarder.function_name
  principal     = "sns.amazonaws.com"
  source_arn    = aws_sns_topic.operations_alarm.arn
}

resource "aws_sns_topic_subscription" "operations_alarm_discord" {
  topic_arn = aws_sns_topic.operations_alarm.arn
  protocol  = "lambda"
  endpoint  = aws_lambda_function.discord_alarm_forwarder.arn

  depends_on = [aws_lambda_permission.allow_operations_alarm_sns]
}
