#!/bin/bash
# Runs on the host before kubelet; never use a workload's credentials or print tokens.
set -Eeuo pipefail
umask 077
export PATH=/usr/local/bin:/usr/bin:/bin
export AWS_MAX_ATTEMPTS=1 AWS_RETRY_MODE=standard AWS_PAGER="" AWS_EC2_METADATA_DISABLED=false
unset AWS_PROFILE AWS_DEFAULT_PROFILE AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN AWS_ROLE_ARN AWS_WEB_IDENTITY_TOKEN_FILE AWS_CONTAINER_CREDENTIALS_FULL_URI AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE
export AWS_EC2_METADATA_SERVICE_ENDPOINT=http://169.254.169.254
command -v aws >/dev/null
command -v curl >/dev/null
meta=http://169.254.169.254/latest
token=$(curl --noproxy '*' -fsS --connect-timeout 2 --max-time 5 -X PUT "$meta/api/token" -H 'X-aws-ec2-metadata-token-ttl-seconds: 60')
[[ -n "$token" ]]
iid=$(curl --noproxy '*' -fsS --connect-timeout 2 --max-time 5 -H "X-aws-ec2-metadata-token: $token" "$meta/meta-data/instance-id")
region=$(curl --noproxy '*' -fsS --connect-timeout 2 --max-time 5 -H "X-aws-ec2-metadata-token: $token" "$meta/meta-data/placement/region")
kind=$(curl --noproxy '*' -fsS --connect-timeout 2 --max-time 5 -H "X-aws-ec2-metadata-token: $token" "$meta/meta-data/instance-type")
unset token
if ! [[ "$iid" =~ ^i-([a-f0-9]{8}|[a-f0-9]{17})$ && "$region" == ap-northeast-2 && "$kind" == t3a.medium ]]; then
  echo "V3 instance identity mismatch; kubelet start denied" >&2
  exit 1
fi
# Never renew a burst: the attempted marker survives reboot and unit restart.
state=/var/lib/yeodam/worker-credit
standard=/usr/local/sbin/yeodam-cpu-credit-standard
timer=yeodam-worker-credit-restore.timer
restore_unit=yeodam-worker-credit-restore.service
install -d -m 0700 "$state"
exec 9>/run/lock/yeodam-worker-credit.lock
flock -x 9
restore() {
  if "$standard"; then
    date +%s >"$state/restored-at"
    systemctl stop "$timer"
    echo 'V3 Worker credit restore verified: standard'
  else
    echo 'V3 Worker credit restore FAILED; independent service will retry' >&2
    return 1
  fi
}
case "${1:-}" in
  restore) restore; exit $? ;;
  prepare) ;;
  *) echo 'Unknown credit operation; denied' >&2; exit 1 ;;
esac
now=$(date +%s)
boot=$(awk '$1=="btime"{print $2}' /proc/stat)
[[ "$now" =~ ^[0-9]+$ && "$boot" =~ ^[0-9]+$ ]]
deadline=$((boot + 600))
if [[ -e "$state/attempted" ]] || ((now < boot || now >= deadline)); then
  restore
  echo 'V3 Worker startup burst already spent or expired; standard retained'
  exit 0
fi
# Atomic create BEFORE API mutation; failures/reboots consume this one attempt.
(set -o noclobber; printf '%s\n' "$deadline" >"$state/attempted")
sync -f "$state/attempted"
# Arm the independent host timer before enabling chargeable CPU mode.
systemctl enable --now "$timer"
systemctl is-enabled --quiet "$timer"
systemctl is-active --quiet "$timer"
# Bound AWS initialization calls; verify Describe, not just Modify HTTP success.
failed=1
for attempt in {1..3}; do
  if (( $(date +%s) >= deadline )); then break; fi
  aws ec2 modify-instance-credit-specification --region "$region" \
    --instance-credit-specifications "InstanceId=$iid,CpuCredits=unlimited" \
    --cli-connect-timeout 3 --cli-read-timeout 5 >/dev/null 2>&1 || true
  if mode=$(aws ec2 describe-instance-credit-specifications --region "$region" --instance-ids "$iid" \
      --query 'InstanceCreditSpecifications[0].CpuCredits' --output text \
      --cli-connect-timeout 3 --cli-read-timeout 5 2>/dev/null) && [[ "$mode" == unlimited ]] && (( $(date +%s) < deadline )); then
    failed=0; break
  fi
  sleep 2
done
if ((failed)); then
  # Release the lock before the independent unit attempts a restoration.
  flock -u 9
  systemctl start --no-block "$restore_unit" || true
  echo 'V3 Worker startup burst unverified; kubelet start denied' >&2
  exit 1
fi
echo 'V3 Worker startup burst verified; Standard restoration timer armed'
