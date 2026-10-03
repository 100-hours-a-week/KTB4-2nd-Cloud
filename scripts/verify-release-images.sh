#!/usr/bin/env bash
set -Eeuo pipefail

source deploy/image-versions.env

for component in FRONTEND BACKEND AI; do
  image_key="${component}_IMAGE"
  digest_key="${component}_DIGEST"
  image="${!image_key}"
  digest="${!digest_key}"
  reference="${image}@${digest}"

  # A platform-specific Pull fails if the selected manifest cannot run on App/Worker x86_64.
  docker pull --platform linux/amd64 "${reference}" >/dev/null

  # The tag must resolve to the exact digest selected by the Cloud manifest.
  tag_digest="$(docker buildx imagetools inspect "${image}" --format '{{.Manifest.Digest}}')"
  [[ "${tag_digest}" == "${digest}" ]] || {
    echo "${component}: SHA tag와 Digest가 다릅니다." >&2
    exit 1
  }
  echo "${component}: Image Digest 및 amd64 검증 완료"
done
