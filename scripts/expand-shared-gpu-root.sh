#!/usr/bin/env bash
# Run only after the reviewed root EBS is expanded to 200 GiB.
set -Eeuo pipefail
[[ "$(id -u)" == 0 ]] || { echo 'Run as root on the shared GPU' >&2; exit 1; }
token=$(curl -fsS --noproxy '*' --connect-timeout 2 --max-time 3 -X PUT \
  -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' http://169.254.169.254/latest/api/token)
instance=$(curl -fsS --noproxy '*' --connect-timeout 2 --max-time 3 \
  -H "X-aws-ec2-metadata-token: $token" http://169.254.169.254/latest/meta-data/instance-id)
[[ "$instance" == i-05e1ad9dd96dba4ae ]] || { echo 'Wrong GPU host' >&2; exit 1; }

root=$(findmnt -n -o SOURCE /)
filesystem=$(findmnt -n -o FSTYPE /)
[[ "$filesystem" == ext4 && "$root" =~ ^/dev/nvme[0-9]+n[0-9]+p1$ ]] || {
  echo 'Unexpected root filesystem or partition; review before resizing' >&2; exit 1;
}
parent=$(lsblk -dn -o PKNAME "$root" | tr -d '[:space:]')
disk="/dev/$parent"
serial=$(lsblk -dn -o SERIAL "$disk" | tr -d '[:space:]-')
[[ "$serial" == vol0fccde58d4fa3e8c8 ]] || { echo 'Wrong root EBS' >&2; exit 1; }
[[ "$(blockdev --getsize64 "$disk")" == 214748364800 ]] || {
  echo 'Root EBS must already be 200 GiB; check AWS modification state' >&2; exit 1;
}
command -v growpart >/dev/null
command -v resize2fs >/dev/null
[[ "${1:-}" == --apply ]] || { echo 'Checks passed; use --apply to extend root partition and ext4'; exit 0; }

# Keep temporary partition metadata in tmpfs even if the root disk is full.
if result=$(TMPDIR=/run growpart "$disk" 1 2>&1); then
  printf '%s\n' "$result"
elif [[ "$result" == NOCHANGE:* ]]; then
  printf '%s\n' "$result"
else
  printf '%s\n' "$result" >&2
  exit 1
fi
resize2fs "$root"
df -hT / /data/qdrant
