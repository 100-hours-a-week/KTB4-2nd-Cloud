#!/usr/bin/env bash
set -Eeuo pipefail

action="${1:-}"
value="${2:-}"
region="ap-northeast-2"
account="483175530259"
cluster="yeodam-v2-staging-app"
service="yeodam-v2-staging-frontend"
family="yeodam-v2-staging-frontend"
target_group="arn:aws:elasticloadbalancing:${region}:${account}:targetgroup/yeodam-v2-staging-fe/376c78d124969a89"
health_url="https://staging.yeodam-2gether.com/api/health"
image_pattern='^ghcr[.]io/100-hours-a-week/yeodam-frontend@sha256:[0-9a-f]{64}$'

if [[ "${action}" != deploy && "${action}" != rollback ]]; then
  echo 'Usage: deploy-v2-staging-frontend.sh deploy IMAGE_DIGEST | rollback TASK_REVISION' >&2
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

  aws ecs wait services-stable --region "${region}" --cluster "${cluster}" \
    --services "${service}" || return 1
  service_state > "${temporary_dir}/verified-service.json"
  jq -e --arg expected "${expected}" \
    '.status == "ACTIVE" and .taskDefinition == $expected and
     .runningCount == .desiredCount and .desiredCount > 0 and
     ([.deployments[] | select(.status == "PRIMARY" and .rolloutState == "COMPLETED")] | length) == 1' \
    "${temporary_dir}/verified-service.json" >/dev/null || return 1
  desired="$(jq -r '.desiredCount' "${temporary_dir}/verified-service.json")"
  healthy="$(aws elbv2 describe-target-health --region "${region}" \
    --target-group-arn "${target_group}" --query 'TargetHealthDescriptions[*].TargetHealth.State' \
    --output json | jq '[.[] | select(. == "healthy")] | length')" || return 1
  (( healthy >= desired )) || return 1

  for attempt in {1..12}; do
    if curl --fail --silent --show-error --max-time 10 --output "${temporary_dir}/health.json" \
      "${health_url}" && jq -e '.status == "ok"' "${temporary_dir}/health.json" >/dev/null; then
      return 0
    fi
    sleep 5
  done
  return 1
}

service_state > "${temporary_dir}/current-service.json"
jq -e '.status == "ACTIVE" and .desiredCount > 0 and
  .runningCount == .desiredCount and .pendingCount == 0 and
  ([.deployments[] | select(.status == "PRIMARY" and .rolloutState == "COMPLETED")] | length) == 1' \
  "${temporary_dir}/current-service.json" >/dev/null || {
  echo 'Frontend service is not stable; resolve its current deployment first.' >&2
  exit 1
}
previous="$(jq -r '.taskDefinition' "${temporary_dir}/current-service.json")"

if [[ "${action}" == deploy ]]; then
  [[ "${value}" =~ ${image_pattern} ]] || {
    echo 'Deploy requires a yeodam-frontend GHCR SHA256 digest reference.' >&2
    exit 2
  }
  aws ecs describe-task-definition --region "${region}" --task-definition "${previous}" \
    --query taskDefinition --output json > "${temporary_dir}/previous-definition.json"
  jq -e --arg family "${family}" \
    '.family == $family and ([.containerDefinitions[].name] == ["frontend"])' \
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
    echo 'Rollback requires a numeric frontend task revision.' >&2
    exit 2
  }
  candidate="arn:aws:ecs:${region}:${account}:task-definition/${family}:${value}"
  aws ecs describe-task-definition --region "${region}" --task-definition "${candidate}" \
    --query taskDefinition --output json > "${temporary_dir}/rollback-definition.json"
  jq -e --arg family "${family}" --arg image_pattern "${image_pattern}" '
    .status == "ACTIVE" and .family == $family and
    ([.containerDefinitions[].name] == ["frontend"]) and
    (.containerDefinitions[0].image | test($image_pattern))
  ' "${temporary_dir}/rollback-definition.json" >/dev/null || {
    echo 'Rollback revision is not an active staging frontend task definition.' >&2
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
