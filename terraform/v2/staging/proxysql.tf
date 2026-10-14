# This document only installs an isolated ProxySQL service on the V1 staging
# App host. It does not change the Backend DataSource or the active DB writer.
resource "aws_ssm_document" "v1_source_proxysql_install" {
  name          = "yeodam-v2-staging-v1-source-proxysql-install"
  document_type = "Command"
  content = jsonencode({
    schemaVersion = "2.2"
    description   = "Install an isolated staging ProxySQL with V1 and V2 MySQL hostgroups"
    mainSteps = [{
      action = "aws:runShellScript"
      name   = "installProxySQL"
      inputs = {
        runCommand = [
          "set -eu",
          "install -d -m 0700 /etc/yeodam/v2-staging-proxysql",
          "printf '%s' '${filebase64("${path.module}/../../../deploy/v2-staging-proxysql-compose.yml")}' | base64 -d > /etc/yeodam/v2-staging-proxysql/compose.yml",
          "chmod 0600 /etc/yeodam/v2-staging-proxysql/compose.yml",
          "temporary=$(mktemp /root/yeodam-proxysql-install.XXXXXX)",
          "trap 'rm -f \"$temporary\"' EXIT",
          "printf '%s' '${filebase64("${path.module}/../../../scripts/install-v2-staging-proxysql.sh")}' | base64 -d > \"$temporary\"",
          "bash -n \"$temporary\"",
          "bash \"$temporary\" '${module.mysql.private_ip}'",
        ]
      }
    }]
  })

  tags = local.common_tags
}

resource "aws_ssm_document" "v1_source_proxysql_check" {
  name          = "yeodam-v2-staging-v1-source-proxysql-check"
  document_type = "Command"
  content = jsonencode({
    schemaVersion = "2.2"
    description   = "Verify V1 and V2 MySQL read paths through the isolated staging ProxySQL"
    mainSteps = [{
      action = "aws:runShellScript"
      name   = "checkProxySQL"
      inputs = {
        runCommand = [
          "set -eu",
          "bash -s '${module.mysql.private_ip}' <<'YEODAM_PROXYSQL_CHECK'\n${file("${path.module}/../../../scripts/check-v2-staging-proxysql.sh")}\nYEODAM_PROXYSQL_CHECK",
        ]
      }
    }]
  })

  tags = local.common_tags
}

resource "aws_ssm_document" "v1_source_backend_db_route" {
  name          = "yeodam-v2-staging-v1-source-backend-db-route"
  document_type = "Command"
  content = jsonencode({
    schemaVersion = "2.2"
    description   = "Switch only the V1 staging Backend between direct V1 MySQL and ProxySQL hostgroup 10"
    parameters = {
      route = {
        type          = "String"
        allowedValues = ["direct", "proxysql"]
      }
      releaseDir = {
        type           = "String"
        allowedPattern = "^/opt/yeodam/releases/[0-9a-f]{40}$"
      }
    }
    mainSteps = [{
      action = "aws:runShellScript"
      name   = "switchBackendDBRoute"
      inputs = {
        runCommand = [
          "set -eu",
          "bash -s -- --route '{{ route }}' --release-dir '{{ releaseDir }}' --image-env /etc/yeodam/v1-source-images.env <<'YEODAM_DB_ROUTE'\n${file("${path.module}/../../../scripts/switch-v1-staging-be-db-route.sh")}\nYEODAM_DB_ROUTE",
        ]
      }
    }]
  })

  tags = local.common_tags
}
