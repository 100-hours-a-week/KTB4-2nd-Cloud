#!/usr/bin/env bash

set -Eeuo pipefail

readonly MOUNT_POINT="/var/lib/mysql"
readonly FILESYSTEM_TYPE="ext4"
readonly FILESYSTEM_LABEL="yeodam_mysql_dat"

volume_id=""
allow_format=false

usage() {
  cat <<'EOF'
Usage:
  sudo ./prepare-mysql-volume.sh \
    --volume-id vol-xxxxxxxxxxxxxxxxx \
    [--format-empty-volume]

Options:
  --volume-id
      Terraform으로 생성한 MySQL Data EBS Volume ID

  --format-empty-volume
      파일시스템이 없는 빈 Volume을 ext4로 포맷하도록 허용

첫 실행에서는 --format-empty-volume이 필요합니다.
이미 ext4 파일시스템이 있는 Volume에는 다시 포맷하지 않습니다.
EOF
}

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --volume-id)
      [[ $# -ge 2 ]] || fail "--volume-id 값이 필요합니다."
      volume_id="$2"
      shift 2
      ;;
    --format-empty-volume)
      allow_format=true
      shift
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
  fail "이 스크립트는 sudo 또는 root 권한으로 실행해야 합니다."
fi

if [[ ! "${volume_id}" =~ ^vol-[0-9a-f]+$ ]]; then
  fail "올바른 EBS Volume ID를 --volume-id로 전달해야 합니다."
fi

for command_name in \
  lsblk \
  findmnt \
  blkid \
  mkfs.ext4 \
  mountpoint \
  mount \
  awk \
  grep \
  find \
  install \
  cp \
  systemctl
do
  command -v "${command_name}" >/dev/null 2>&1 \
    || fail "필요한 명령을 찾을 수 없습니다: ${command_name}"
done

expected_serial="${volume_id//-/}"
expected_serial="${expected_serial,,}"

target_device=""

while read -r device_name device_serial device_type; do
  [[ "${device_type}" == "disk" ]] || continue

  normalized_serial="${device_serial//-/}"
  normalized_serial="${normalized_serial,,}"

  if [[ "${normalized_serial}" == "${expected_serial}" ]]; then
    if [[ -n "${target_device}" ]]; then
      fail "같은 Volume ID에 해당하는 장치를 여러 개 찾았습니다."
    fi

    target_device="/dev/${device_name}"
  fi
done < <(lsblk -dn -o NAME,SERIAL,TYPE)

if [[ -z "${target_device}" ]]; then
  fail "연결된 장치에서 ${volume_id}를 찾지 못했습니다."
fi

echo "확인된 MySQL Data EBS: ${target_device} (${volume_id})"

root_source="$(findmnt -n -o SOURCE /)"
root_parent="$(lsblk -no PKNAME "${root_source}" | head -n 1)"

if [[ -n "${root_parent}" ]]; then
  root_device="/dev/${root_parent}"
else
  root_device="${root_source}"
fi

if [[ "${target_device}" == "${root_device}" ]]; then
  fail "대상 장치가 Root EBS입니다. 포맷을 중단합니다."
fi

child_device_count="$(
  lsblk -nr -o NAME "${target_device}" |
    awk 'END { print NR }'
)"

if [[ "${child_device_count}" -ne 1 ]]; then
  fail "대상 장치에 Partition 또는 하위 Block Device가 있습니다. 수동 확인이 필요합니다."
fi

current_mount="$(
  lsblk -dn -o MOUNTPOINT "${target_device}" |
    awk 'NF { print; exit }'
)"

if [[ -n "${current_mount}" && "${current_mount}" != "${MOUNT_POINT}" ]]; then
  fail "대상 장치가 예상하지 않은 위치에 마운트되어 있습니다: ${current_mount}"
fi

filesystem_type="$(blkid -s TYPE -o value "${target_device}" 2>/dev/null || true)"

partition_table_type="$(
  blkid -p -s PTTYPE -o value "${target_device}" 2>/dev/null || true
)"

if [[ -z "${filesystem_type}" && -n "${partition_table_type}" ]]; then
  fail "파일시스템은 없지만 ${partition_table_type} Partition Table이 존재합니다. 수동 확인이 필요합니다."
fi

if [[ -z "${filesystem_type}" ]]; then
  if [[ "${allow_format}" != true ]]; then
    fail "파일시스템이 없습니다. 빈 Volume이 맞는지 확인한 뒤 --format-empty-volume을 사용하세요."
  fi

  echo "${target_device}에 ${FILESYSTEM_TYPE} 파일시스템을 생성합니다."
  mkfs.ext4 -L "${FILESYSTEM_LABEL}" "${target_device}"

  filesystem_type="$(blkid -s TYPE -o value "${target_device}")"
fi

if [[ "${filesystem_type}" != "${FILESYSTEM_TYPE}" ]]; then
  fail "예상하지 않은 파일시스템입니다: ${filesystem_type}"
fi

filesystem_uuid="$(blkid -s UUID -o value "${target_device}")"

if [[ -z "${filesystem_uuid}" ]]; then
  fail "파일시스템 UUID를 확인하지 못했습니다."
fi

install -d -m 0755 "${MOUNT_POINT}"

if mountpoint -q "${MOUNT_POINT}"; then
  mounted_source="$(
    findmnt -n -o SOURCE --target "${MOUNT_POINT}"
  )"

  mounted_uuid="$(
    blkid -s UUID -o value "${mounted_source}" 2>/dev/null || true
  )"

  if [[ "${mounted_uuid}" != "${filesystem_uuid}" ]]; then
    fail "${MOUNT_POINT}에 다른 Filesystem이 마운트되어 있습니다: ${mounted_source}"
  fi
else
  existing_file="$(
    find "${MOUNT_POINT}" -mindepth 1 -maxdepth 1 -print -quit
  )"

  if [[ -n "${existing_file}" ]]; then
    fail "${MOUNT_POINT}가 비어 있지 않습니다. 기존 파일을 먼저 확인하세요."
  fi
fi

existing_mount_entry="$(
  awk -v target="${MOUNT_POINT}" '
    $1 !~ /^#/ && $2 == target { print }
  ' /etc/fstab
)"

expected_fstab_source="UUID=${filesystem_uuid}"

if [[ -n "${existing_mount_entry}" ]]; then
  if ! grep -Fq "${expected_fstab_source}" <<<"${existing_mount_entry}"; then
    fail "/etc/fstab에 ${MOUNT_POINT}의 다른 Mount 설정이 존재합니다."
  fi
else
  if [[ ! -e "/etc/fstab.before-yeodam-mysql" ]]; then
    cp /etc/fstab "/etc/fstab.before-yeodam-mysql"
  fi

  printf '%s %s %s %s %s %s\n' \
    "${expected_fstab_source}" \
    "${MOUNT_POINT}" \
    "${FILESYSTEM_TYPE}" \
    "defaults,nofail,x-systemd.device-timeout=60s" \
    "0" \
    "2" \
    >> /etc/fstab
fi

systemctl daemon-reload

if ! mountpoint -q "${MOUNT_POINT}"; then
  mount "${MOUNT_POINT}"
fi

install -d -m 0755 /etc/systemd/system/mysql.service.d

cat > /etc/systemd/system/mysql.service.d/10-require-data-mount.conf <<EOF
[Unit]
RequiresMountsFor=${MOUNT_POINT}
EOF

systemctl daemon-reload

echo
echo "MySQL Data EBS 준비 완료"
findmnt "${MOUNT_POINT}"
lsblk -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINTS,SERIAL "${target_device}"