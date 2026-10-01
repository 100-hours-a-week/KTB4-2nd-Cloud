#!/usr/bin/env bash
set -euo pipefail

script_directory="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
load_directory="$(cd "${script_directory}/.." && pwd -P)"
work_directory="$(mktemp -d)"
cleanup() {
  rm -rf -- "${work_directory}"
}
trap cleanup EXIT

mkdir -p "${work_directory}/photos/camera" "${work_directory}/photos/phone"

for index in 1 2; do
  printf '\xff\xd8\xff%s' "${index}" >"${work_directory}/photos/camera/camera-${index}.JPG"
done

for index in 1 2 3 4; do
  printf 'heic%s' "${index}" >"${work_directory}/photos/phone/phone-${index}.HEIC"
done

for index in 5 6 7 8; do
  printf '\xff\xd8\xff%s' "${index}" >"${work_directory}/photos/phone/phone-${index}.jpg"
done

printf 'ignored' >"${work_directory}/photos/.DS_Store"

first_manifest="${work_directory}/manifest-first.json"
second_manifest="${work_directory}/manifest-second.json"

"${load_directory}/scripts/generate-manifest.sh" \
  "${work_directory}/photos" \
  5 \
  "${first_manifest}" \
  test-v1
"${load_directory}/scripts/generate-manifest.sh" \
  "${work_directory}/photos" \
  5 \
  "${second_manifest}" \
  test-v1

"${load_directory}/scripts/validate-manifest.sh" "${first_manifest}"
cmp "${first_manifest}" "${second_manifest}"

jq -e '.name == "yeodam-baseline-5" and .version == "test-v1" and (.files | length) == 5' \
  "${first_manifest}" >/dev/null
jq -e '[.files[].path | select(contains("/camera/"))] | length == 1' \
  "${first_manifest}" >/dev/null
jq -e '[.files[].path | select(contains("/phone/"))] | length == 4' \
  "${first_manifest}" >/dev/null

if "${load_directory}/scripts/generate-manifest.sh" \
  "${work_directory}/photos" \
  11 \
  "${work_directory}/too-many.json" \
  test-v1 >/dev/null 2>&1; then
  echo "Generator accepted more photos than the dataset contains." >&2
  exit 1
fi

echo "Manifest script tests passed."
