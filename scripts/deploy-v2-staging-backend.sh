#!/usr/bin/env bash
set -Eeuo pipefail

action="${1:-}"
value="${2:-}"
region="ap-northeast-2"
account="483175530259"
cluster="yeodam-v2-staging-app"
service="yeodam-v2-staging-backend"
family="yeodam-v2-staging-backend"
target_group_name="yeodam-v2-staging-be"
health_url="https://staging.yeodam-2gether.com/api/actuator/health"
image_pattern='^ghcr[.]io/100-hours-a-week/yeodam-backend@sha256:[0-9a-f]{64}$'

if [[ "${action}" != deploy && "${action}" != rollback ]]; then
  echo 'Usage: deploy-v2-staging-backend.sh deploy IMAGE_DIGEST | rollback TASK_REVISION' >&2
  exit 2
fi
if [[ "${AWS_REGION:-${AWS_DEFAULT_REGION:-${region}}}" != "${region}" ]]; then
  echo "Expected AWS Region ${region}." >&2
  exit 2
fi
if [[ "$(aws sts get-caller-identity --query Account --output text)" != "${account}" ]]; then
  echo "Expected AWS Account ${account}." >&2
  exit 2
fi

temporary_dir="$(mktemp -d)"
trap 'rm -rf -- "${temporary_dir}"' EXIT

service_state() {
  aws ecs describe-services --region "${region}" --cluster "${cluster}" \
    --services "${service}" --query 'services[0]' --output json
}

verify_healthy() {
  local expected="$1"
  local desired
  local healthy
  local attempt
  local service_summary

  # ECS may report the desired running count before the rollout is COMPLETED.
  for attempt in {1..60}; do
    if ! service_state > "${temporary_dir}/verified-service.json"; then
      echo 'Could not read backend service state.' >&2
      return 1
    fi
    if ! jq -e --arg expected "${expected}" '
      .status == "ACTIVE" and .taskDefinition == $expected and
      .runningCount == .desiredCount and .desiredCount > 0 and .pendingCount == 0 and
      (.deployments | length) == 1 and
      .deployments[0].status == "PRIMARY" and
      .deployments[0].rolloutState == "COMPLETED"
    ' "${temporary_dir}/verified-service.json" >/dev/null; then
      service_summary="$(jq -r '
        "task=\(.taskDefinition // "none") running=\(.runningCount // 0)/\(.desiredCount // 0) pending=\(.pendingCount // 0) deployments=\([.deployments[]? | "\(.status):\(.rolloutState // "UNKNOWN")"] | join(","))"
      ' "${temporary_dir}/verified-service.json")"
      echo "Waiting for ECS rollout (${attempt}/60): ${service_summary}" >&2
    else
      desired="$(jq -r '.desiredCount' "${temporary_dir}/verified-service.json")"
      if ! aws elbv2 describe-target-health --region "${region}" \
        --target-group-arn "${target_group}" \
        --query 'TargetHealthDescriptions[*].TargetHealth.State' --output json \
        > "${temporary_dir}/target-health.json"; then
        echo 'Could not read backend target health.' >&2
        return 1
      fi
      healthy="$(jq '[.[] | select(. == "healthy")] | length' "${temporary_dir}/target-health.json")"
      if (( healthy < desired )); then
        echo "Waiting for ALB targets (${attempt}/60): healthy=${healthy}/${desired}" >&2
      elif curl --fail --silent --show-error --max-time 10 \
        --output "${temporary_dir}/health.json" "${health_url}" &&
        jq -e '.status == "UP"' "${temporary_dir}/health.json" >/dev/null; then
        return 0
      else
        echo "Waiting for HTTPS health (${attempt}/60): ${health_url}" >&2
      fi
    fi
    if (( attempt < 60 )); then
      sleep 10
    fi
  done
  echo "Backend did not pass ECS, ALB and HTTPS checks after 60 attempts: ${expected}" >&2
  return 1
}

service_state > "${temporary_dir}/current-service.json"
jq -e '.status == "ACTIVE" and .desiredCount > 0 and
  .runningCount == .desiredCount and .pendingCount == 0 and
  ([.deployments[] | select(.status == "PRIMARY" and .rolloutState == "COMPLETED")] | length) == 1' \
  "${temporary_dir}/current-service.json" >/dev/null || {
  echo 'Backend service is absent or not stable; provision and verify its first Task before using this workflow.' >&2
  exit 1
}
previous="$(jq -r '.taskDefinition' "${temporary_dir}/current-service.json")"
target_group="$(aws elbv2 describe-target-groups --region "${region}" \
  --names "${target_group_name}" --query 'TargetGroups[0].TargetGroupArn' --output text)"
if [[ "${target_group}" != arn:aws:elasticloadbalancing:${region}:${account}:targetgroup/${target_group_name}/* ]]; then
  echo "Unexpected staging backend target group: ${target_group}" >&2
  exit 1
fi
aws ecs describe-task-definition --region "${region}" --task-definition "${previous}" \
  --query taskDefinition --output json > "${temporary_dir}/previous-definition.json"

if [[ "${action}" == deploy ]]; then
  [[ "${value}" =~ ${image_pattern} ]] || {
    echo 'Deploy requires a yeodam-backend GHCR SHA256 digest reference.' >&2
    exit 2
  }
  jq -e --arg family "${family}" \
    '.family == $family and ([.containerDefinitions[].name] == ["backend"])' \
    "${temporary_dir}/previous-definition.json" >/dev/null
  jq --arg image "${value}" '
    {family, taskRoleArn, executionRoleArn, networkMode, containerDefinitions,
     volumes, placementConstraints, requiresCompatibilities, cpu, memory,
     pidMode, ipcMode, proxyConfiguration, inferenceAccelerators,
     ephemeralStorage, runtimePlatform, enableFaultInjection}
    | with_entries(select(.value != null))
    | .containerDefinitions[0].image = $image
  ' "${temporary_dir}/previous-definition.json" > "${temporary_dir}/candidate-definition.json"
  candidate="$(aws ecs register-task-definition --region "${region}" \
    --cli-input-json "file://${temporary_dir}/candidate-definition.json" \
    --query 'taskDefinition.taskDefinitionArn' --output text)"
else
  [[ "${value}" =~ ^[1-9][0-9]*$ ]] || {
    echo 'Rollback requires a numeric backend task revision.' >&2
    exit 2
  }
  candidate="arn:aws:ecs:${region}:${account}:task-definition/${family}:${value}"
  aws ecs describe-task-definition --region "${region}" --task-definition "${candidate}" \
    --query taskDefinition --output json > "${temporary_dir}/rollback-definition.json"
  jq -e --arg family "${family}" --arg image_pattern "${image_pattern}" \
    --arg execution_role "$(jq -r '.executionRoleArn' "${temporary_dir}/previous-definition.json")" \
    --arg task_role "$(jq -r '.taskRoleArn' "${temporary_dir}/previous-definition.json")" '
    .status == "ACTIVE" and .family == $family and
    ([.containerDefinitions[].name] == ["backend"]) and
    .executionRoleArn == $execution_role and .taskRoleArn == $task_role and
    (.containerDefinitions[0].image | test($image_pattern))
  ' "${temporary_dir}/rollback-definition.json" >/dev/null || {
    echo 'Rollback revision is not an active staging backend task definition.' >&2
    exit 2
  }
fi

if [[ "${candidate}" == "${previous}" ]]; then
  echo "Already running ${candidate}; no deployment needed."
  exit 0
fi

echo "Previous: ${previous}"
echo "Candidate: ${candidate}"
if ! aws ecs update-service --region "${region}" --cluster "${cluster}" \
  --service "${service}" --task-definition "${candidate}" --output json \
  > "${temporary_dir}/update.json"; then
  echo 'ECS rejected the update; checking the previous revision.' >&2
fi

if verify_healthy "${candidate}"; then
  echo "Deployment healthy: ${candidate}"
  exit 0
fi

echo "Candidate did not become healthy; restoring ${previous}." >&2
service_state > "${temporary_dir}/failed-service.json"
if [[ "$(jq -r '.taskDefinition' "${temporary_dir}/failed-service.json")" != "${previous}" ]]; then
  aws ecs update-service --region "${region}" --cluster "${cluster}" \
    --service "${service}" --task-definition "${previous}" --output json \
    > "${temporary_dir}/restore.json"
fi
if verify_healthy "${previous}"; then
  echo "Previous revision restored: ${previous}" >&2
else
  echo "Automatic restore could not be verified; inspect ECS events and targets." >&2
fi
exit 1
