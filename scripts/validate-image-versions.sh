#!/usr/bin/env bash

set -Eeuo pipefail

image_env="${1:-deploy/image-versions.env}"

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

read_value() {
  local key="$1"
  local value

  value="$(awk -F= -v key="${key}" '$1 == key { count++; value = substr($0, index($0, "=") + 1) } END { if (count == 1) print value }' "${image_env}")"
  [[ -n "${value}" ]] || fail "${key} 값을 하나만 지정해야 합니다."
  printf '%s' "${value}"
}

validate_pair() {
  local image_key="$1"
  local digest_key="$2"
  local package="$3"
  local image digest

  image="$(read_value "${image_key}")"
  digest="$(read_value "${digest_key}")"

  [[ "${image}" =~ ^ghcr\.io/100-hours-a-week/${package}:sha-[0-9a-f]{40}$ ]] \
    || fail "${image_key} 형식이 올바르지 않습니다."
  [[ "${digest}" =~ ^sha256:[0-9a-f]{64}$ ]] \
    || fail "${digest_key} 형식이 올바르지 않습니다."
}

[[ -f "${image_env}" ]] || fail "Image Version 파일을 찾을 수 없습니다: ${image_env}"

validate_pair FRONTEND_IMAGE FRONTEND_DIGEST yeodam-frontend
validate_pair BACKEND_IMAGE BACKEND_DIGEST yeodam-backend
validate_pair AI_IMAGE AI_DIGEST yeodam-ai-worker

echo "Image Version 검증 완료: ${image_env}"
