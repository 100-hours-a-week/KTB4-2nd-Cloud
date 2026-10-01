#!/usr/bin/env bash
set -euo pipefail

photo_archive="${1:-}"
s3_prefix="${2:-}"
run_id="${3:-$(date -u '+%Y%m%dT%H%M%SZ')}"

if [[ -z "${photo_archive}" || -z "${s3_prefix}" ]]; then
  echo "Usage: $0 /absolute/path/to/eval.zip s3://BUCKET/load-test-fixtures RUN_ID" >&2
  exit 2
fi

if [[ ! -f "${photo_archive}" || "${photo_archive}" != /* ]]; then
  echo "Photo archive must be an existing absolute path: ${photo_archive}" >&2
  exit 1
fi

if [[ ! "${s3_prefix}" =~ ^s3://[^/]+/load-test-fixtures/?$ ]]; then
  echo "S3 prefix must use s3://BUCKET/load-test-fixtures." >&2
  exit 1
fi

if [[ ! "${run_id}" =~ ^[A-Za-z0-9._-]+$ ]]; then
  echo "RUN_ID may contain only letters, numbers, dot, underscore, and hyphen." >&2
  exit 1
fi

for command_name in aws tar; do
  command -v "${command_name}" >/dev/null 2>&1 || {
    echo "Required command not found: ${command_name}" >&2
    exit 1
  }
done

if command -v sha256sum >/dev/null 2>&1; then
  hash_command=(sha256sum)
elif command -v shasum >/dev/null 2>&1; then
  hash_command=(shasum -a 256)
else
  echo "sha256sum or shasum is required." >&2
  exit 1
fi

script_directory="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
repository_root="$(cd "${script_directory}/../../.." && pwd -P)"
work_directory="$(mktemp -d)"
cleanup() {
  rm -rf -- "${work_directory}"
}
trap cleanup EXIT

bundle_path="${work_directory}/load-test-source.tar.gz"
setup_script="${repository_root}/tests/load/scripts/setup-generator-workspace.sh"
tar_options=()
if [[ "$(uname -s)" == "Darwin" ]]; then
  tar_options=(--no-xattrs)
fi

COPYFILE_DISABLE=1 tar "${tar_options[@]}" \
  --exclude='tests/load/config/*.env' \
  --exclude='tests/load/fixtures/*.json' \
  --exclude='tests/load/results/*' \
  -czf "${bundle_path}" \
  -C "${repository_root}" \
  tests/load

photo_hash="$("${hash_command[@]}" "${photo_archive}" | awk '{ print $1 }')"
bundle_hash="$("${hash_command[@]}" "${bundle_path}" | awk '{ print $1 }')"
setup_hash="$("${hash_command[@]}" "${setup_script}" | awk '{ print $1 }')"
printf '%s  eval.zip\n' "${photo_hash}" >"${work_directory}/eval.zip.sha256"
printf '%s  load-test-source.tar.gz\n' "${bundle_hash}" >"${work_directory}/load-test-source.tar.gz.sha256"
printf '%s  setup-generator-workspace.sh\n' "${setup_hash}" >"${work_directory}/setup-generator-workspace.sh.sha256"

destination="${s3_prefix%/}/${run_id}"
aws s3 cp "${photo_archive}" "${destination}/eval.zip" --sse AES256 --only-show-errors
aws s3 cp "${work_directory}/eval.zip.sha256" "${destination}/eval.zip.sha256" --sse AES256 --only-show-errors
aws s3 cp "${bundle_path}" "${destination}/load-test-source.tar.gz" --sse AES256 --only-show-errors
aws s3 cp "${work_directory}/load-test-source.tar.gz.sha256" "${destination}/load-test-source.tar.gz.sha256" --sse AES256 --only-show-errors
aws s3 cp "${setup_script}" "${destination}/setup-generator-workspace.sh" --sse AES256 --only-show-errors
aws s3 cp "${work_directory}/setup-generator-workspace.sh.sha256" "${destination}/setup-generator-workspace.sh.sha256" --sse AES256 --only-show-errors

echo "Generator assets uploaded: ${destination}"
