#!/usr/bin/env bash
set -euo pipefail

manifest_path="${1:-}"

if [[ -z "${manifest_path}" ]]; then
  echo "Usage: $0 /absolute/path/to/manifest.json" >&2
  exit 2
fi

if [[ ! -f "${manifest_path}" ]]; then
  echo "Manifest not found: ${manifest_path}" >&2
  exit 1
fi

if ! command -v jq >/dev/null 2>&1; then
  echo "jq is required to validate the dataset manifest." >&2
  exit 1
fi

jq -e '
  (.files | type == "array") and
  (.files | length >= 1 and length <= 200) and
  (all(.files[];
    (.path | type == "string" and startswith("/")) and
    ((.contentType // "image/jpeg") | test("^image/[A-Za-z0-9.+-]+$"))
  ))
' "${manifest_path}" >/dev/null

file_count=0
total_bytes=0
while IFS=$'\t' read -r path expected_size; do
  if [[ ! -f "${path}" ]]; then
    echo "Photo not found: ${path}" >&2
    exit 1
  fi

  actual_size="$(stat -f '%z' "${path}" 2>/dev/null || stat -c '%s' "${path}")"
  if (( actual_size < 1 || actual_size > 15728640 )); then
    echo "Photo size must be between 1B and 15MiB: ${path} (${actual_size}B)" >&2
    exit 1
  fi
  if [[ "${expected_size}" != "null" && "${expected_size}" != "${actual_size}" ]]; then
    echo "sizeBytes mismatch: ${path} expected=${expected_size} actual=${actual_size}" >&2
    exit 1
  fi

  file_count=$((file_count + 1))
  total_bytes=$((total_bytes + actual_size))
done < <(jq -r '.files[] | [.path, (.sizeBytes // null)] | @tsv' "${manifest_path}")

if (( total_bytes > 3221225472 )); then
  echo "Dataset exceeds 3GiB: ${total_bytes}B" >&2
  exit 1
fi

echo "Manifest valid: files=${file_count} total_bytes=${total_bytes}"
