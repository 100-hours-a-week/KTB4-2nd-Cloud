resource "aws_iam_openid_connect_provider" "github_actions" {
  url = "https://token.actions.githubusercontent.com"

  client_id_list = [
    "sts.amazonaws.com",
  ]

  tags = {
    Name = "${local.name_prefix}-github-actions-oidc"
    Role = "deployment"
  }
}

data "aws_iam_policy_document" "github_actions_deploy_assume_role" {
  statement {
    sid     = "AllowGitHubActionsProduction"
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type = "Federated"
      identifiers = [
        aws_iam_openid_connect_provider.github_actions.arn,
      ]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = [local.github_actions_deploy_subject]
    }
  }
}

resource "aws_iam_role" "github_actions_deploy" {
  name                 = "${local.name_prefix}-github-actions-deploy-role"
  description          = "Deployment role for the Yeodam Cloud production GitHub Actions environment"
  assume_role_policy   = data.aws_iam_policy_document.github_actions_deploy_assume_role.json
  max_session_duration = 3600

  tags = {
    Name = "${local.name_prefix}-github-actions-deploy-role"
    Role = "deployment"
  }
}

data "aws_iam_policy_document" "github_actions_deploy" {
  statement {
    sid    = "DescribeDeploymentInstances"
    effect = "Allow"

    actions = [
      "ec2:DescribeInstances",
      "ec2:DescribeInstanceStatus",
    ]

    resources = ["*"]

    condition {
      test     = "StringEquals"
      variable = "aws:RequestedRegion"
      values   = [var.aws_region]
    }
  }

  statement {
    sid    = "ControlWorkerInstance"
    effect = "Allow"

    actions = [
      "ec2:StartInstances",
      "ec2:StopInstances",
    ]

    resources = [
      aws_instance.worker.arn,
    ]
  }

  statement {
    sid    = "RunDeploymentCommands"
    effect = "Allow"

    actions = [
      "ssm:SendCommand",
    ]

    resources = [
      "arn:${data.aws_partition.current.partition}:ssm:${var.aws_region}::document/AWS-RunShellScript",
      aws_instance.app.arn,
      aws_instance.worker.arn,
    ]
  }

  statement {
    sid    = "ReadDeploymentCommandResults"
    effect = "Allow"

    actions = [
      "ssm:DescribeInstanceInformation",
      "ssm:GetCommandInvocation",
      "ssm:ListCommandInvocations",
      "ssm:ListCommands",
    ]

    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "github_actions_deploy" {
  name   = "${local.name_prefix}-github-actions-deploy"
  role   = aws_iam_role.github_actions_deploy.id
  policy = data.aws_iam_policy_document.github_actions_deploy.json
}
