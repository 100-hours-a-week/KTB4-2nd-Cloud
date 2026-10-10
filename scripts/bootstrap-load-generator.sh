#!/usr/bin/env bash

set -Eeuo pipefail

readonly K6_VERSION="2.3.0"
readonly K6_ARCHIVE="k6-v${K6_VERSION}-linux-amd64.tar.gz"
readonly K6_ARCHIVE_URL="https://github.com/grafana/k6/releases/download/v${K6_VERSION}/${K6_ARCHIVE}"
readonly K6_ARCHIVE_SHA256="39c3117b6af817592dcd0ce4242105c0a7af10948c2a425306f0be8f7a8a8ab1"
readonly AWS_CLI_ARCHIVE_URL="https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip"
readonly WORKSPACE="/opt/yeodam-load"

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

log() {
  echo
  echo "==> $*"
}

[[ "${EUID}" -eq 0 ]] || fail "이 스크립트는 root 권한으로 실행해야 합니다."

# shellcheck disable=SC1091
source /etc/os-release
[[ "${ID}" == "ubuntu" && "${VERSION_ID}" == "24.04" ]] ||
  fail "Ubuntu 24.04에서만 실행할 수 있습니다: ${ID} ${VERSION_ID}"
[[ "$(dpkg --print-architecture)" == "amd64" ]] ||
  fail "amd64 Architecture에서만 실행할 수 있습니다."

readonly work_directory="$(mktemp -d)"
cleanup() {
  rm -rf -- "${work_directory}"
}
trap cleanup EXIT

log "기본 Package 설치"

export DEBIAN_FRONTEND=noninteractive
# The reviewed Ubuntu AMI defaults to HTTP apt mirrors. The staging load
# generator permits HTTPS egress only, including during first boot.
sed -i '/^URIs:/ s#http://#https://#g' /etc/apt/sources.list.d/ubuntu.sources
apt-get update
apt-get install --yes --no-install-recommends \
  ca-certificates \
  curl \
  jq \
  tar \
  time \
  unzip

log "AWS CLI v2 설치"

if ! command -v aws >/dev/null 2>&1; then
  curl \
    --fail \
    --silent \
    --show-error \
    --location \
    "${AWS_CLI_ARCHIVE_URL}" \
    --output "${work_directory}/awscliv2.zip"
  unzip -q "${work_directory}/awscliv2.zip" -d "${work_directory}"
  "${work_directory}/aws/install" \
    --install-dir /usr/local/aws-cli \
    --bin-dir /usr/local/bin
fi

aws --version

log "k6 v${K6_VERSION} 설치"

curl \
  --fail \
  --silent \
  --show-error \
  --location \
  "${K6_ARCHIVE_URL}" \
  --output "${work_directory}/${K6_ARCHIVE}"

printf '%s  %s\n' \
  "${K6_ARCHIVE_SHA256}" \
  "${work_directory}/${K6_ARCHIVE}" |
  sha256sum --check --status - || fail "k6 Archive SHA256 검증에 실패했습니다."

tar -xzf "${work_directory}/${K6_ARCHIVE}" -C "${work_directory}"
install \
  --owner=root \
  --group=root \
  --mode=0755 \
  "${work_directory}/k6-v${K6_VERSION}-linux-amd64/k6" \
  /usr/local/bin/k6

k6_version="$(k6 version | head -n 1)"
[[ "${k6_version}" == *"v${K6_VERSION}"* ]] ||
  fail "설치된 k6 Version이 다릅니다: ${k6_version}"

log "부하 테스트 Workspace 준비"

install --directory --owner=ubuntu --group=ubuntu --mode=0750 \
  "${WORKSPACE}" \
  "${WORKSPACE}/data" \
  "${WORKSPACE}/manifests" \
  "${WORKSPACE}/results" \
  "${WORKSPACE}/source"

printf '%s\n' \
  "k6_version=${k6_version}" \
  "prepared_at=$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
  >"${WORKSPACE}/bootstrap.txt"
chown ubuntu:ubuntu "${WORKSPACE}/bootstrap.txt"

echo
echo "V1 k6 부하 발생기 준비 완료"
echo "Workspace: ${WORKSPACE}"
