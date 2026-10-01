#!/usr/bin/env bash
set -euo pipefail

s3_run_prefix="${1:-}"
dataset_version="${2:-}"
workspace="${3:-/opt/yeodam-load}"

if [[ -z "${s3_run_prefix}" || -z "${dataset_version}" ]]; then
  echo "Usage: $0 s3://BUCKET/load-test-fixtures/RUN_ID VERSION [/opt/yeodam-load]" >&2
  exit 2
fi

if [[ ! "${s3_run_prefix}" =~ ^s3://[^/]+/load-test-fixtures/[A-Za-z0-9._-]+/?$ ]]; then
  echo "Invalid load-test fixture S3 run prefix: ${s3_run_prefix}" >&2
  exit 1
fi

for command_name in aws jq k6 sha256sum tar unzip; do
  command -v "${command_name}" >/dev/null 2>&1 || {
    echo "Required command not found: ${command_name}" >&2
    exit 1
  }
done

if [[ -n "$(find "${workspace}/data" "${workspace}/source" -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
  echo "Workspace data or source directory is not empty: ${workspace}" >&2
  exit 1
fi

s3_run_prefix="${s3_run_prefix%/}"
aws s3 cp "${s3_run_prefix}/eval.zip" "${workspace}/data/eval.zip" --only-show-errors
aws s3 cp "${s3_run_prefix}/eval.zip.sha256" "${workspace}/data/eval.zip.sha256" --only-show-errors
aws s3 cp "${s3_run_prefix}/load-test-source.tar.gz" "${workspace}/source/load-test-source.tar.gz" --only-show-errors
aws s3 cp "${s3_run_prefix}/load-test-source.tar.gz.sha256" "${workspace}/source/load-test-source.tar.gz.sha256" --only-show-errors

(
  cd "${workspace}/data"
  sha256sum --check eval.zip.sha256
  unzip -q eval.zip
)

(
  cd "${workspace}/source"
  sha256sum --check load-test-source.tar.gz.sha256
  tar -xzf load-test-source.tar.gz
)

dataset_root="$(find "${workspace}/data" -mindepth 1 -maxdepth 1 -type d ! -name '__MACOSX' -print -quit)"
if [[ -z "${dataset_root}" ]]; then
  echo "Extracted photo directory was not found in ${workspace}/data." >&2
  exit 1
fi

"${workspace}/source/tests/load/scripts/prepare-baseline-manifests.sh" \
  "${dataset_root}" \
  "${workspace}/manifests" \
  "${dataset_version}"

if [[ "${EUID}" -eq 0 ]] && id ubuntu >/dev/null 2>&1; then
  chown -R ubuntu:ubuntu "${workspace}"
fi

echo "Generator workspace ready: ${workspace}"
