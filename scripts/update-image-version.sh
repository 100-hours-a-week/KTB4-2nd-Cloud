#!/usr/bin/env bash

set -Eeuo pipefail

readonly MANIFEST_PATH="deploy/image-versions.env"

component=""
source_repository=""
source_sha=""
image=""
digest=""
temporary_file=""

usage() {
  cat <<'EOF'
Usage:
  ./scripts/update-image-version.sh \
    --component frontend|backend|ai \
    --source-repository OWNER/REPOSITORY \
    --source-sha COMMIT_SHA \
    --image GHCR_IMAGE \
    --digest SHA256_DIGEST
EOF
}

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

cleanup() {
  [[ -z "${temporary_file}" ]] || rm -f -- "${temporary_file}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --component)
      component="${2:-}"
      shift 2
      ;;
    --source-repository)
      source_repository="${2:-}"
      shift 2
      ;;
    --source-sha)
      source_sha="${2:-}"
      shift 2
      ;;
    --image)
      image="${2:-}"
      shift 2
      ;;
    --digest)
      digest="${2:-}"
      shift 2
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      fail "알 수 없는 옵션입니다: $1"
      ;;
  esac
done

[[ -f "${MANIFEST_PATH}" ]] || fail "Manifest를 찾을 수 없습니다: ${MANIFEST_PATH}"
[[ "${source_sha}" =~ ^[0-9a-f]{40}$ ]] || fail "Source Commit은 40자리 SHA여야 합니다."
[[ "${digest}" =~ ^sha256:[0-9a-f]{64}$ ]] || fail "Image Digest 형식이 올바르지 않습니다."

case "${component}" in
  frontend)
    expected_repository="100-hours-a-week/KTB4-2nd-FE"
    expected_package="yeodam-frontend"
    image_key="FRONTEND_IMAGE"
    digest_key="FRONTEND_DIGEST"
    ;;
  backend)
    expected_repository="100-hours-a-week/KTB4-2nd-BE"
    expected_package="yeodam-backend"
    image_key="BACKEND_IMAGE"
    digest_key="BACKEND_DIGEST"
    ;;
  ai)
    expected_repository="100-hours-a-week/KTB4-2nd-AI"
    expected_package="yeodam-ai-worker"
    image_key="AI_IMAGE"
    digest_key="AI_DIGEST"
    ;;
  *)
    fail "component는 frontend, backend 또는 ai여야 합니다."
    ;;
esac

[[ "${source_repository}" == "${expected_repository}" ]] \
  || fail "component와 Source Repository가 일치하지 않습니다."
[[ "${image}" == "ghcr.io/100-hours-a-week/${expected_package}:sha-${source_sha}" ]] \
  || fail "Image가 Source Commit 또는 허용된 GHCR Package와 일치하지 않습니다."

temporary_file="$(mktemp)"
trap cleanup EXIT

awk \
  -v image_key="${image_key}" \
  -v digest_key="${digest_key}" \
  -v image="${image}" \
  -v digest="${digest}" '
    $0 ~ "^" image_key "=" {
      print image_key "=" image
      image_count++
      next
    }
    $0 ~ "^" digest_key "=" {
      print digest_key "=" digest
      digest_count++
      next
    }
    { print }
    END {
      if (image_count != 1 || digest_count != 1) {
        exit 1
      }
    }
  ' "${MANIFEST_PATH}" > "${temporary_file}" \
  || fail "Manifest에서 ${component} 항목을 하나만 찾아야 합니다."

cp "${temporary_file}" "${MANIFEST_PATH}"
chmod 0644 "${MANIFEST_PATH}"
bash scripts/validate-image-versions.sh "${MANIFEST_PATH}"

echo "${component} Image Manifest 갱신 완료: ${source_sha}"
