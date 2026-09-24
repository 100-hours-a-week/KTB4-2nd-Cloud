#!/usr/bin/env bash

set -Eeuo pipefail

readonly MYSQL_MOUNT_POINT="/var/lib/mysql"
readonly SWAP_FILE="/swapfile"
readonly SWAP_SIZE_BYTES="2147483648"
readonly SWAPPINESS="10"

readonly DOCKER_VERSION="5:29.8.1-1~ubuntu.24.04~noble"
readonly AWS_CLI_INSTALL_SCRIPT_URL="https://awscli.amazonaws.com/v2/install.sh"

readonly MYSQL_APT_CONFIG_VERSION="0.8.40-1"
readonly MYSQL_APT_CONFIG_MD5="981ff0a16aab27a0cd97f4c4ee49e9fd"
readonly MYSQL_APT_CONFIG_URL="https://dev.mysql.com/get/mysql-apt-config_${MYSQL_APT_CONFIG_VERSION}_all.deb"

readonly CLOUDWATCH_AGENT_URL="https://amazoncloudwatch-agent.s3.amazonaws.com/ubuntu/amd64/latest/amazon-cloudwatch-agent.deb"

mysql_volume_uuid=""

usage() {
  cat <<'EOF'
Usage:
  sudo ./bootstrap-app-host.sh \
    --mysql-volume-uuid UUID

Options:
  --mysql-volume-uuid
      /var/lib/mysql에 마운트된 MySQL Data EBS의 Filesystem UUID
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
    --mysql-volume-uuid)
      [[ $# -ge 2 ]] || fail "--mysql-volume-uuid 값이 필요합니다."
      mysql_volume_uuid="$2"
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

if [[ "${EUID}" -ne 0 ]]; then
  fail "이 스크립트는 root 권한으로 실행해야 합니다."
fi

if [[ -z "${mysql_volume_uuid}" ]]; then
  fail "--mysql-volume-uuid를 지정해야 합니다."
fi

for command_name in \
  apt-cache \
  apt-get \
  awk \
  bash \
  blkid \
  chmod \
  cp \
  curl \
  dpkg \
  dpkg-query \
  fallocate \
  find \
  findmnt \
  grep \
  install \
  md5sum \
  mktemp \
  mkswap \
  mountpoint \
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

log "MySQL Data EBS Mount 확인"

if ! mountpoint -q "${MYSQL_MOUNT_POINT}"; then
  fail "${MYSQL_MOUNT_POINT}가 별도 Filesystem으로 마운트되어 있지 않습니다."
fi

mysql_mount_source="$(
  findmnt -n -o SOURCE --target "${MYSQL_MOUNT_POINT}"
)"

actual_mysql_uuid="$(
  blkid -s UUID -o value "${mysql_mount_source}" 2>/dev/null || true
)"

if [[ "${actual_mysql_uuid}" != "${mysql_volume_uuid}" ]]; then
  fail "MySQL EBS UUID가 일치하지 않습니다. expected=${mysql_volume_uuid}, actual=${actual_mysql_uuid}"
fi

echo "MySQL Mount Source: ${mysql_mount_source}"
echo "MySQL Filesystem UUID: ${actual_mysql_uuid}"

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
  debconf-utils \
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

log "Nginx 설치와 임시 Maintenance 응답 구성"

apt-get install --yes --no-install-recommends nginx

if [[ -L /etc/nginx/sites-enabled/default ]]; then
  unlink /etc/nginx/sites-enabled/default
elif [[ -e /etc/nginx/sites-enabled/default ]]; then
  fail "/etc/nginx/sites-enabled/default가 Symbolic Link가 아닙니다. 수동 확인이 필요합니다."
fi

cat > /etc/nginx/sites-available/yeodam-maintenance <<'EOF'
server {
    listen 80 default_server;
    server_name _;

    default_type text/plain;
    return 503 "Yeodam service is not deployed yet.\n";
}
EOF

ln -sfn \
  /etc/nginx/sites-available/yeodam-maintenance \
  /etc/nginx/sites-enabled/yeodam-maintenance

nginx -t
systemctl enable nginx

if systemctl is-active --quiet nginx; then
  systemctl reload nginx
else
  systemctl start nginx
fi

log "MySQL 9.7 LTS 공식 APT Repository 구성"

mysql_apt_config_package="${work_directory}/mysql-apt-config.deb"

curl \
  --fail \
  --silent \
  --show-error \
  --location \
  "${MYSQL_APT_CONFIG_URL}" \
  --output "${mysql_apt_config_package}"

printf '%s  %s\n' \
  "${MYSQL_APT_CONFIG_MD5}" \
  "${mysql_apt_config_package}" |
  md5sum --check --status \
  || fail "mysql-apt-config Package MD5가 일치하지 않습니다."

printf '%s\n' \
  "mysql-apt-config mysql-apt-config/select-server select mysql-9.7-lts" \
  "mysql-apt-config mysql-apt-config/select-product select Ok" |
  debconf-set-selections

dpkg --install "${mysql_apt_config_package}"

apt-get update

mysql_candidate_version="$(
  apt-cache policy mysql-community-server |
    awk '/Candidate:/ { print $2 }'
)"

if [[ ! "${mysql_candidate_version}" =~ ^9\.7([.-]|$) ]]; then
  fail "MySQL 9.7 Package를 선택하지 못했습니다: ${mysql_candidate_version}"
fi

if ! package_installed mysql-server; then
  mapfile -t mysql_directory_entries < <(
    find "${MYSQL_MOUNT_POINT}" \
      -mindepth 1 \
      -maxdepth 1 \
      -printf '%f\n'
  )

  for entry in "${mysql_directory_entries[@]}"; do
    if [[ "${entry}" != "lost+found" ]]; then
      fail "MySQL 설치 전 데이터 경로에 예상하지 않은 항목이 있습니다: ${entry}"
    fi
  done

  if [[ -d "${MYSQL_MOUNT_POINT}/lost+found" ]]; then
    rmdir "${MYSQL_MOUNT_POINT}/lost+found" \
      || fail "lost+found가 비어 있지 않아 제거하지 못했습니다."
  fi

  printf '%s\n' \
    "mysql-community-server mysql-community-server/root-pass password" \
    "mysql-community-server mysql-community-server/re-root-pass password" |
    debconf-set-selections

  apt-get install --yes --no-install-recommends mysql-server
fi

systemctl enable --now mysql

installed_mysql_version="$(
  mysql \
    --protocol=socket \
    --user=root \
    --batch \
    --skip-column-names \
    --execute='SELECT VERSION();'
)"

if [[ ! "${installed_mysql_version}" =~ ^9\.7([.-]|$) ]]; then
  fail "설치된 MySQL Version이 9.7이 아닙니다: ${installed_mysql_version}"
fi

log "CloudWatch Agent Package 설치"

if ! package_installed amazon-cloudwatch-agent; then
  cloudwatch_agent_package="${work_directory}/amazon-cloudwatch-agent.deb"

  curl \
    --fail \
    --silent \
    --show-error \
    --location \
    "${CLOUDWATCH_AGENT_URL}" \
    --output "${cloudwatch_agent_package}"

  dpkg --install "${cloudwatch_agent_package}" \
    || apt-get install --fix-broken --yes
fi

# 실제 Metric과 Log 수집 설정은 별도 작업에서 적용합니다.
systemctl disable --now amazon-cloudwatch-agent 2>/dev/null || true

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

echo "--- Nginx ---"
nginx -v
nginx -t
systemctl is-active nginx

nginx_http_status="$(
  curl \
    --silent \
    --output /dev/null \
    --write-out '%{http_code}' \
    http://127.0.0.1/
)"

echo "HTTP Status: ${nginx_http_status}"

if [[ "${nginx_http_status}" != "503" ]]; then
  fail "Nginx Maintenance 응답이 503이 아닙니다: ${nginx_http_status}"
fi

echo "--- MySQL ---"
mysql --version
echo "Server: ${installed_mysql_version}"
findmnt "${MYSQL_MOUNT_POINT}"
systemctl is-active mysql

echo "--- CloudWatch Agent ---"
/opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent --version
systemctl is-enabled amazon-cloudwatch-agent 2>/dev/null || true
systemctl is-active amazon-cloudwatch-agent 2>/dev/null || true

echo
echo "App EC2 Host 기본 Package 설치 완료"
