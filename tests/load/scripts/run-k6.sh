#!/usr/bin/env bash
set -euo pipefail

script_path="${1:-}"

if [[ -z "${script_path}" ]]; then
  echo "Usage: $0 scenarios/upload-baseline.js" >&2
  exit 2
fi

if [[ ! -f "${script_path}" ]]; then
  echo "k6 script not found: ${script_path}" >&2
  exit 1
fi

if ! command -v k6 >/dev/null 2>&1; then
  echo "k6 v2.3.0 is required but was not found in PATH." >&2
  exit 1
fi

k6_version="$(k6 version | head -n 1)"
if [[ "${k6_version}" != *"v2.3.0"* ]]; then
  echo "Expected k6 v2.3.0, found: ${k6_version}" >&2
  exit 1
fi

run_id="$(date -u '+%Y%m%dT%H%M%SZ')"
result_root="${K6_RESULTS_DIRECTORY:-results}"
result_dir="${result_root%/}/${run_id}"
mkdir -p "${result_dir}"

export K6_SUMMARY_PATH="${result_dir}/summary.json"
k6_args=(--no-color)
if [[ "${script_path}" == scenarios/migration-write.js || "${script_path}" == scenarios/migration-trip-create.js ]]; then
  [[ -z "${K6_HTTP_DEBUG:-}" ]] || { echo 'Disable K6_HTTP_DEBUG to keep session credentials out of logs.' >&2; exit 2; }
  event_name="probe-events.jsonl"
  [[ "${script_path}" != scenarios/migration-trip-create.js ]] || event_name="trip-events.jsonl"
  k6_args+=(--console-output "${result_dir}/${event_name}" --log-format raw)
  if [[ "${script_path}" == scenarios/migration-write.js && "${K6_PROBE_PROMETHEUS:-0}" == 1 ]]; then
    export K6_PROMETHEUS_RW_SERVER_URL='http://plg.staging.yeodam.internal:9090/api/v1/write'
    export K6_PROMETHEUS_RW_TREND_STATS='p(95),p(99)'
    export K6_PROMETHEUS_RW_STALE_MARKERS=true
    curl --fail --silent --show-error --max-time 5 'http://plg.staging.yeodam.internal:9090/-/ready' >/dev/null
    k6_args+=(--out experimental-prometheus-rw)
  elif [[ "${K6_PROBE_PROMETHEUS:-0}" != 0 ]]; then
    echo 'K6_PROBE_PROMETHEUS must be 0 or 1.' >&2; exit 2
  fi
fi

network_totals() {
  local direction="$1"
  local total=0
  local stats_path

  if [[ ! -d /sys/class/net ]]; then
    echo "unavailable"
    return
  fi

  for stats_path in /sys/class/net/*/statistics/"${direction}_bytes"; do
    [[ "${stats_path}" == */lo/* ]] && continue
    total=$((total + $(<"${stats_path}")))
  done
  echo "${total}"
}

network_rx_before="$(network_totals rx)"
network_tx_before="$(network_totals tx)"

set +e
if [[ -x /usr/bin/time ]] && /usr/bin/time --version 2>&1 | grep -qi 'GNU time'; then
  /usr/bin/time \
    --verbose \
    --output="${result_dir}/runner-time.txt" \
    k6 run "${k6_args[@]}" "${script_path}" 2>&1 |
    tee "${result_dir}/run.log"
  k6_status="${PIPESTATUS[0]}"
else
  k6 run "${k6_args[@]}" "${script_path}" 2>&1 | tee "${result_dir}/run.log"
  k6_status="${PIPESTATUS[0]}"
  echo "GNU time unavailable; runner-time.txt was not created." >"${result_dir}/runner-time-unavailable.txt"
fi
set -e

network_rx_after="$(network_totals rx)"
network_tx_after="$(network_totals tx)"

printf '%s\n' \
  "run_id=${run_id}" \
  "script=${script_path}" \
  "k6_version=${k6_version}" \
  "network_rx_bytes_before=${network_rx_before}" \
  "network_rx_bytes_after=${network_rx_after}" \
  "network_tx_bytes_before=${network_tx_before}" \
  "network_tx_bytes_after=${network_tx_after}" \
  "exit_code=${k6_status}" >"${result_dir}/execution.txt"

echo "Result directory: ${result_dir}"
exit "${k6_status}"
