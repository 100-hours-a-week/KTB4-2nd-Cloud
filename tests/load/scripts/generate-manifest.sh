#!/usr/bin/env bash
set -euo pipefail

dataset_root="${1:-}"
photo_count="${2:-}"
output_path="${3:-}"
dataset_version="${4:-}"

if [[ -z "${dataset_root}" || -z "${photo_count}" || -z "${output_path}" || -z "${dataset_version}" ]]; then
  echo "Usage: $0 /absolute/path/to/photos COUNT /absolute/path/to/manifest.json VERSION" >&2
  exit 2
fi

if [[ ! -d "${dataset_root}" ]]; then
  echo "Dataset directory not found: ${dataset_root}" >&2
  exit 1
fi

if [[ "${dataset_root}" != /* || "${output_path}" != /* ]]; then
  echo "Dataset and output paths must be absolute." >&2
  exit 1
fi

if [[ ! "${photo_count}" =~ ^[0-9]+$ ]] || (( photo_count < 1 || photo_count > 200 )); then
  echo "COUNT must be an integer between 1 and 200." >&2
  exit 1
fi

if ! command -v jq >/dev/null 2>&1; then
  echo "jq is required to generate the dataset manifest." >&2
  exit 1
fi

if command -v sha256sum >/dev/null 2>&1; then
  hash_command=(sha256sum)
elif command -v shasum >/dev/null 2>&1; then
  hash_command=(shasum -a 256)
else
  echo "sha256sum or shasum is required to select photos deterministically." >&2
  exit 1
fi

file_size() {
  if stat --version >/dev/null 2>&1; then
    stat -c '%s' "$1"
  else
    stat -f '%z' "$1"
  fi
}

dataset_root="$(cd "${dataset_root}" && pwd -P)"
output_directory="$(dirname "${output_path}")"
mkdir -p "${output_directory}"
output_directory="$(cd "${output_directory}" && pwd -P)"
output_path="${output_directory}/$(basename "${output_path}")"

entries_file="$(mktemp)"
group_sorted_file="$(mktemp)"
prioritized_file="$(mktemp)"
sorted_file="$(mktemp)"
selected_file="$(mktemp)"
output_file="$(mktemp "${output_directory}/.manifest.XXXXXX")"
cleanup() {
  rm -f \
    "${entries_file}" \
    "${group_sorted_file}" \
    "${prioritized_file}" \
    "${sorted_file}" \
    "${selected_file}" \
    "${output_file}"
}
trap cleanup EXIT

while IFS= read -r -d '' photo_path; do
  relative_path="${photo_path#"${dataset_root}"/}"
  if [[ "${relative_path}" == *$'\n'* || "${relative_path}" == *$'\r'* || "${relative_path}" == *$'\t'* || "${relative_path}" == *'"'* ]]; then
    echo "Unsupported character in photo path: ${relative_path}" >&2
    exit 1
  fi

  extension="$(printf '%s' "${photo_path##*.}" | tr '[:upper:]' '[:lower:]')"
  case "${extension}" in
    jpg | jpeg)
      content_type="image/jpeg"
      ;;
    heic | heif)
      content_type="image/heic"
      ;;
    png)
      content_type="image/png"
      ;;
    *)
      continue
      ;;
  esac

  size_bytes="$(file_size "${photo_path}")"
  if (( size_bytes < 1 || size_bytes > 15728640 )); then
    echo "Photo size must be between 1B and 15MiB: ${photo_path} (${size_bytes}B)" >&2
    exit 1
  fi

  selection_key="$(printf '%s' "${relative_path}" | "${hash_command[@]}" | awk '{ print $1 }')"
  if [[ "${relative_path}" == */* ]]; then
    source_group="${relative_path%%/*}"
  else
    source_group="__root__"
  fi
  printf '%s\t%s\t%s\t%s\t%s\n' \
    "${source_group}" "${selection_key}" "${photo_path}" "${content_type}" "${size_bytes}" >>"${entries_file}"
done < <(find "${dataset_root}" -type f -print0)

available_count="$(wc -l <"${entries_file}" | tr -d ' ')"
if (( available_count < photo_count )); then
  echo "Not enough supported photos: requested=${photo_count} available=${available_count}" >&2
  exit 1
fi

LC_ALL=C sort -t $'\t' -k1,1 -k2,2 "${entries_file}" >"${group_sorted_file}"
LC_ALL=C awk -F $'\t' '
  NR == FNR {
    totals[$1]++
    next
  }
  {
    ranks[$1]++
    priority = (ranks[$1] - 0.5) / totals[$1]
    printf "%.12f\t%s\n", priority, $0
  }
' "${group_sorted_file}" "${group_sorted_file}" >"${prioritized_file}"
LC_ALL=C sort -t $'\t' -k1,1n -k3,3 "${prioritized_file}" >"${sorted_file}"
sed -n "1,${photo_count}p" "${sorted_file}" >"${selected_file}"

dataset_name="yeodam-baseline-${photo_count}"
jq -Rn \
  --arg name "${dataset_name}" \
  --arg version "${dataset_version}" \
  '{
    name: $name,
    version: $version,
    files: [
      inputs
      | split("\t")
      | {
          path: .[3],
          filename: (.[3] | split("/") | last),
          contentType: .[4],
          sizeBytes: (.[5] | tonumber)
        }
    ]
  }' <"${selected_file}" >"${output_file}"

mv "${output_file}" "${output_path}"
echo "Manifest generated: path=${output_path} files=${photo_count} version=${dataset_version}"
