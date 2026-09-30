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
result_dir="results/${run_id}"
mkdir -p "${result_dir}"

export K6_SUMMARY_PATH="${result_dir}/summary.json"

set +e
k6 run --no-color "${script_path}" 2>&1 | tee "${result_dir}/run.log"
k6_status="${PIPESTATUS[0]}"
set -e

printf '%s\n' \
  "run_id=${run_id}" \
  "script=${script_path}" \
  "k6_version=${k6_version}" \
  "exit_code=${k6_status}" >"${result_dir}/execution.txt"

echo "Result directory: ${result_dir}"
exit "${k6_status}"
