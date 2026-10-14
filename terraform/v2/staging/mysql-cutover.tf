locals {
  cutover_parameter_prefix = "/yeodam/v2/staging/migration"
  cutover_parameter_arns = [
    for suffix in ["MYSQL_TARGET_PASSWORD", "TARGET_CA_PEM"] :
    "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter${local.cutover_parameter_prefix}/${suffix}"
  ]
}

data "aws_iam_policy_document" "cutover_target_parameters" {
  statement {
    actions   = ["ssm:GetParameter", "ssm:PutParameter"]
    resources = local.cutover_parameter_arns
  }
}

resource "aws_iam_role_policy" "cutover_target_parameters" {
  name   = "yeodam-v2-staging-mysql-cutover-parameters"
  role   = module.mysql.instance_role_name
  policy = data.aws_iam_policy_document.cutover_target_parameters.json
}

data "aws_iam_policy_document" "cutover_source_parameters" {
  statement {
    actions   = ["ssm:GetParameter"]
    resources = local.cutover_parameter_arns
  }
}

resource "aws_iam_role_policy" "cutover_source_parameters" {
  name   = "yeodam-v2-staging-v1-source-cutover-parameters"
  role   = aws_iam_role.v1_source_app.name
  policy = data.aws_iam_policy_document.cutover_source_parameters.json
}

resource "aws_ssm_document" "mysql_cutover_account" {
  name          = "yeodam-v2-staging-mysql-cutover-account"
  document_type = "Command"
  content = jsonencode({
    schemaVersion = "2.2"
    description   = "Provision a TLS-only control account on the staging replica"
    mainSteps = [{
      action = "aws:runShellScript"
      name   = "provisionCutoverAccount"
      inputs = { runCommand = [
        "export SOURCE_HOST='${aws_instance.v1_source_app.private_ip}'",
        "bash -s <<'YEODAM_CUTOVER_ACCOUNT'\n${file("${path.module}/../../../scripts/provision-staging-cutover-account.sh")}\nYEODAM_CUTOVER_ACCOUNT",
      ] }
    }]
  })
  tags = local.common_tags
}

# Installs code only. Actual mutations require a matching Run UUID and are
# invoked locally on the V1 staging App to avoid SSM latency inside the gap.
resource "aws_ssm_document" "mysql_cutover_install" {
  name          = "yeodam-v2-staging-mysql-cutover-install"
  document_type = "Command"
  content = jsonencode({
    schemaVersion = "2.2"
    description   = "Install the staging handoff controller without changing DB roles"
    mainSteps = [{
      action = "aws:runShellScript"
      name   = "installCutoverController"
      inputs = { runCommand = [
        "set -eu",
        "test -f /etc/yeodam/v2-staging-proxysql/admin-password",
        "printf '%s' '${filebase64("${path.module}/../../../scripts/staging-db-cutover.py")}' | base64 -d > /usr/local/sbin/yeodam-staging-db-cutover",
        "chmod 0750 /usr/local/sbin/yeodam-staging-db-cutover",
        "python3 -m py_compile /usr/local/sbin/yeodam-staging-db-cutover",
      ] }
    }]
  })
  tags = local.common_tags
}
