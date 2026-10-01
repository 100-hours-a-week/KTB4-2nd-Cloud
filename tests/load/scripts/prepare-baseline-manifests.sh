#!/usr/bin/env bash
set -euo pipefail

dataset_root="${1:-}"
output_directory="${2:-}"
dataset_version="${3:-}"

if [[ -z "${dataset_root}" || -z "${output_directory}" || -z "${dataset_version}" ]]; then
  echo "Usage: $0 /absolute/path/to/photos /absolute/path/to/manifests VERSION" >&2
  exit 2
fi

script_directory="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
mkdir -p "${output_directory}"
output_directory="$(cd "${output_directory}" && pwd -P)"

for photo_count in 10 11 30 150; do
  manifest_path="${output_directory}/baseline-${photo_count}.json"
  "${script_directory}/generate-manifest.sh" \
    "${dataset_root}" \
    "${photo_count}" \
    "${manifest_path}" \
    "${dataset_version}"
  "${script_directory}/validate-manifest.sh" "${manifest_path}"
done
