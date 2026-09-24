#!/usr/bin/env bash

set -Eeuo pipefail

readonly SWAP_FILE="/swapfile"
readonly SWAP_SIZE_BYTES="2147483648"
readonly SWAPPINESS="10"

readonly DOCKER_VERSION="5:29.8.1-1~ubuntu.24.04~noble"
readonly AWS_CLI_INSTALL_SCRIPT_URL="https://awscli.amazonaws.com/v2/install.sh"

usage() {
  cat <<'EOF'
Usage:
  sudo ./bootstrap-worker-host.sh

AI Worker EC2에 2GiB Swap과 Docker Engine/Compose를 설치합니다.
인자는 없습니다.
EOF
}

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

log() {
  echo
  echo "==> $*"
}

package_installed() {
  local package_name="$1"

  dpkg-query \
    --show \
    --showformat='${db:Status-Status}' \
    "${package_name}" 2>/dev/null |
    grep -qx "installed"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --help|-h)
      usage
      exit 0
      ;;
    *)
      fail "알 수 없는 옵션입니다: $1"
      ;;
  esac
done

if [[ "${EUID}" -ne 0 ]]; then
  fail "이 스크립트는 root 권한으로 실행해야 합니다."
fi

for command_name in \
  apt-cache \
  apt-get \
  awk \
  bash \
  chmod \
  cp \
  curl \
  dpkg \
  dpkg-query \
  fallocate \
  grep \
  install \
  mkswap \
  mktemp \
  rm \
  stat \
  swapon \
  sysctl \
  systemctl
do
  command -v "${command_name}" >/dev/null 2>&1 \
    || fail "필요한 명령을 찾을 수 없습니다: ${command_name}"
done

readonly work_directory="$(mktemp -d)"

cleanup() {
  if [[ -n "${work_directory:-}" && -d "${work_directory}" ]]; then
    rm -rf -- "${work_directory}"
  fi
}

trap cleanup EXIT

log "운영체제와 Architecture 확인"

# shellcheck disable=SC1091
source /etc/os-release

if [[ "${ID}" != "ubuntu" || "${VERSION_ID}" != "24.04" ]]; then
  fail "지원하지 않는 운영체제입니다: ${ID} ${VERSION_ID}"
fi

architecture="$(dpkg --print-architecture)"

if [[ "${architecture}" != "amd64" ]]; then
  fail "지원하지 않는 Architecture입니다: ${architecture}"
fi

echo "OS: ${PRETTY_NAME}"
echo "Architecture: ${architecture}"

log "2GiB Swap 구성"

if [[ -e "${SWAP_FILE}" ]]; then
  swap_file_size="$(stat -c '%s' "${SWAP_FILE}")"

  if [[ "${swap_file_size}" != "${SWAP_SIZE_BYTES}" ]]; then
    fail "기존 ${SWAP_FILE}의 크기가 2GiB가 아닙니다: ${swap_file_size}"
  fi
else
  fallocate -l 2G "${SWAP_FILE}"
  chmod 600 "${SWAP_FILE}"
  mkswap "${SWAP_FILE}"
fi

chmod 600 "${SWAP_FILE}"

if ! swapon --noheadings --show=NAME | grep -Fxq "${SWAP_FILE}"; then
  swapon "${SWAP_FILE}"
fi

mapfile -t swap_fstab_entries < <(
  awk -v source="${SWAP_FILE}" '
    $1 == source { print }
  ' /etc/fstab
)

if [[ "${#swap_fstab_entries[@]}" -gt 1 ]]; then
  fail "/etc/fstab에 ${SWAP_FILE} 항목이 여러 개 존재합니다."
fi

if [[ "${#swap_fstab_entries[@]}" -eq 1 ]]; then
  read -r \
    existing_swap_source \
    existing_swap_target \
    existing_swap_type \
    _ \
    <<<"${swap_fstab_entries[0]}"

  if [[ "${existing_swap_target}" != "none" ||
        "${existing_swap_type}" != "swap" ]]; then
    fail "/etc/fstab의 기존 ${SWAP_FILE} 항목이 예상 형식과 다릅니다."
  fi
else
  if [[ ! -e "/etc/fstab.before-yeodam-swap" ]]; then
    cp /etc/fstab "/etc/fstab.before-yeodam-swap"
  fi

  printf '%s none swap sw 0 0\n' "${SWAP_FILE}" >> /etc/fstab
fi

cat > /etc/sysctl.d/99-yeodam-swap.conf <<EOF
vm.swappiness = ${SWAPPINESS}
EOF

sysctl -w "vm.swappiness=${SWAPPINESS}"

log "APT 기본 Package 설치"

export DEBIAN_FRONTEND=noninteractive

apt-get update

apt-get install --yes --no-install-recommends \
  ca-certificates \
  curl \
  gnupg \
  unzip

log "AWS CLI v2 설치"

if ! command -v aws >/dev/null 2>&1; then
  aws_cli_install_script="${work_directory}/install-aws-cli.sh"

  curl \
    --fail \
    --silent \
    --show-error \
    --location \
    "${AWS_CLI_INSTALL_SCRIPT_URL}" \
    --output "${aws_cli_install_script}"

  chmod 0700 "${aws_cli_install_script}"
  bash "${aws_cli_install_script}" --system
fi

aws --version

log "Docker 공식 APT Repository 구성"

for conflicting_package in \
  docker.io \
  docker-compose \
  docker-compose-v2 \
  docker-doc \
  docker-buildx \
  podman-docker \
  containerd \
  runc
do
  if package_installed "${conflicting_package}"; then
    fail "Docker 공식 Package와 충돌 가능한 Package가 설치되어 있습니다: ${conflicting_package}"
  fi
done

install -m 0755 -d /etc/apt/keyrings

curl \
  --fail \
  --silent \
  --show-error \
  --location \
  "https://download.docker.com/linux/ubuntu/gpg" \
  --output "${work_directory}/docker.asc"

install \
  -m 0644 \
  "${work_directory}/docker.asc" \
  /etc/apt/keyrings/docker.asc

cat > /etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: noble
Components: stable
Architectures: amd64
Signed-By: /etc/apt/keyrings/docker.asc
EOF

apt-get update

available_docker_versions="$(
  apt-cache madison docker-ce |
    awk '{ print $3 }'
)"

if ! grep -Fxq "${DOCKER_VERSION}" <<<"${available_docker_versions}"; then
  fail "지정한 Docker Version을 공식 Repository에서 찾지 못했습니다: ${DOCKER_VERSION}"
fi

apt-get install --yes --no-install-recommends \
  "docker-ce=${DOCKER_VERSION}" \
  "docker-ce-cli=${DOCKER_VERSION}" \
  containerd.io \
  docker-buildx-plugin \
  docker-compose-plugin

systemctl enable --now docker

log "설치 결과"

echo "--- Swap ---"
swapon --show
sysctl vm.swappiness

echo "--- Docker ---"
docker version --format 'Server: {{.Server.Version}}'
docker compose version
systemctl is-active docker

echo "--- AWS CLI ---"
aws --version

echo
echo "AI Worker EC2 Host 기본 Package 설치 완료"
