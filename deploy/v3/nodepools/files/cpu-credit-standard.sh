#!/bin/bash
# Runs on the host before kubelet; never use a workload's credentials or print tokens.
set -Eeuo pipefail
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
for attempt in {1..12}; do
  # A successful Modify API response may still contain Unsuccessful entries;
  # trust only the subsequent Describe result for this exact instance.
  aws ec2 modify-instance-credit-specification --region "$region" \
    --instance-credit-specifications "InstanceId=$iid,CpuCredits=standard" \
    --cli-connect-timeout 3 --cli-read-timeout 5 >/dev/null 2>&1 || true
  if mode=$(aws ec2 describe-instance-credit-specifications --region "$region" --instance-ids "$iid" \
      --query 'InstanceCreditSpecifications[0].CpuCredits' --output text \
      --cli-connect-timeout 3 --cli-read-timeout 5 2>/dev/null) && [[ "$mode" == standard ]]; then
    echo 'V3 CPU credit mode verified: standard'
    exit 0
  fi
  sleep 5
done
echo 'V3 CPU credit mode unverified; kubelet start denied' >&2
exit 1
