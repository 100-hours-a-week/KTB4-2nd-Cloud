# A one-time consistent source snapshot is written to the existing private
# staging backup bucket. Only the rehearsal App may write this prefix.
data "aws_iam_policy_document" "v1_source_mysql_snapshot" {
  statement {
    sid     = "WriteOwnReplicationSnapshot"
    actions = ["s3:PutObject", "s3:GetObject", "s3:AbortMultipartUpload"]
    resources = [
      "${aws_s3_bucket.mysql_backup.arn}/migration/v1-source/*",
    ]
  }
}

resource "aws_iam_role_policy" "v1_source_mysql_snapshot" {
  name   = "yeodam-v2-staging-v1-source-mysql-snapshot"
  role   = aws_iam_role.v1_source_app.id
  policy = data.aws_iam_policy_document.v1_source_mysql_snapshot.json
}

resource "aws_ssm_document" "mysql_replication_source_snapshot" {
  name          = "yeodam-v2-staging-mysql-source-snapshot"
  document_type = "Command"
  content = jsonencode({
    schemaVersion = "2.2"
    description   = "Capture the V1 staging application schema and replication coordinates"
    mainSteps = [{
      action = "aws:runShellScript"
      name   = "captureSourceSnapshot"
      inputs = {
        runCommand = [
          "export BACKUP_BUCKET='${aws_s3_bucket.mysql_backup.id}'",
          "bash -s <<'YEODAM_SOURCE_SNAPSHOT'\n${file("${path.module}/files/capture-mysql-replication-source.sh")}\nYEODAM_SOURCE_SNAPSHOT",
        ]
      }
    }]
  })

  tags = local.common_tags
}

# The reset confirmation and snapshot key are supplied at invocation time.
# Terraform state and this document never contain database passwords or data.
resource "aws_ssm_document" "mysql_replication_initialize" {
  name          = "yeodam-v2-staging-mysql-initialize-replica"
  document_type = "Command"
  content = jsonencode({
    schemaVersion = "2.2"
    description   = "Back up V2 staging, initialize its V1-compatible schema, then start TLS replication"
    parameters = {
      snapshotReadyKey = {
        type           = "String"
        description    = "Ready marker produced by the V1 staging source snapshot"
        allowedPattern = "^migration/v1-source/[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}[.]ready$"
      }
      confirmDestinationReset = {
        type           = "String"
        description    = "Explicitly confirm reset of the V1-compatible schema after backup"
        allowedPattern = "^RESET_STAGING_V1_COMPAT_SCHEMA_AFTER_BACKUP$"
      }
    }
    mainSteps = [{
      action = "aws:runShellScript"
      name   = "initializeReplica"
      inputs = {
        runCommand = [
          "export BACKUP_BUCKET='${aws_s3_bucket.mysql_backup.id}'",
          "export SOURCE_HOST='${aws_instance.v1_source_app.private_ip}'",
          "export SNAPSHOT_READY_KEY='{{ snapshotReadyKey }}'",
          "export CONFIRM_DESTINATION_RESET='{{ confirmDestinationReset }}'",
          "bash -s <<'YEODAM_REPLICA_INITIALIZE'\n${file("${path.module}/files/initialize-mysql-replica.sh")}\nYEODAM_REPLICA_INITIALIZE",
        ]
      }
    }]
  })

  tags = local.common_tags
}

# The existing host predates the fixed relay-log names in the bootstrap
# template. Apply this once before checking restart persistence.
resource "aws_ssm_document" "mysql_replication_stable_relay_log" {
  name          = "yeodam-v2-staging-mysql-stable-relay-log"
  document_type = "Command"
  content = jsonencode({
    schemaVersion = "2.2"
    description   = "Pin MySQL relay log filenames so Docker container hostname changes do not break replication"
    mainSteps = [{
      action = "aws:runShellScript"
      name   = "ensureStableRelayLog"
      inputs = {
        runCommand = [
          "bash -s <<'YEODAM_STABLE_RELAY_LOG'\n${file("${path.module}/files/ensure-mysql-stable-relay-log.sh")}\nYEODAM_STABLE_RELAY_LOG",
        ]
      }
    }]
  })

  tags = local.common_tags
}

resource "aws_ssm_document" "mysql_replication_persistent_filters" {
  name          = "yeodam-v2-staging-mysql-persistent-replication-filters"
  document_type = "Command"
  content = jsonencode({
    schemaVersion = "2.2"
    description   = "Keep the V1-compatible table filter after MySQL restart"
    parameters = {
      confirmFilterMigration = {
        type           = "String"
        default        = "NO_MIGRATION"
        allowedPattern = "^(NO_MIGRATION|MIGRATE_V1_COMPAT_FILTER_AFTER_BACKUP)$"
      }
    }
    mainSteps = [{
      action = "aws:runShellScript"
      name   = "ensurePersistentFilters"
      inputs = {
        runCommand = [
          "export SOURCE_HOST='${aws_instance.v1_source_app.private_ip}'",
          "export CONFIRM_FILTER_MIGRATION='{{ confirmFilterMigration }}'",
          "bash -s <<'YEODAM_PERSISTENT_FILTERS'\n${file("${path.module}/files/ensure-mysql-replication-filters.sh")}\nYEODAM_PERSISTENT_FILTERS",
        ]
      }
    }]
  })

  tags = local.common_tags
}
