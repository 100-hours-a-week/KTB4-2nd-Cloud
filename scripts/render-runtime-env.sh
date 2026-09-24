#!/usr/bin/env bash

set -Eeuo pipefail
umask 077

readonly DEFAULT_REGION="ap-northeast-2"
readonly DEFAULT_PATH_PREFIX="/yeodam/v1"

scope=""
output_path=""
aws_region="${AWS_REGION:-${DEFAULT_REGION}}"
path_prefix="${DEFAULT_PATH_PREFIX}"
temporary_directory=""

usage() {
  cat <<'EOF'
Usage:
  sudo ./render-runtime-env.sh \
    --scope app|worker \
    [--output PATH] \
    [--region AWS_REGION] \
    [--path-prefix PARAMETER_PATH]

Defaults:
  app output:    /run/yeodam/app.env
  worker output: /run/yeodam/worker.env
  region:        ap-northeast-2
  path prefix:   /yeodam/v1

The EC2 instance role reads the selected Parameter Store path with decryption.
Parameter values are never printed.
EOF
}

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

cleanup() {
  if [[ -n "${temporary_directory}" && -d "${temporary_directory}" ]]; then
    rm -rf -- "${temporary_directory}"
  fi
}

trap cleanup EXIT

while [[ $# -gt 0 ]]; do
  case "$1" in
    --scope)
      [[ $# -ge 2 ]] || fail "--scope 값이 필요합니다."
      scope="$2"
      shift 2
      ;;
    --output)
      [[ $# -ge 2 ]] || fail "--output 값이 필요합니다."
      output_path="$2"
      shift 2
      ;;
    --region)
      [[ $# -ge 2 ]] || fail "--region 값이 필요합니다."
      aws_region="$2"
      shift 2
      ;;
    --path-prefix)
      [[ $# -ge 2 ]] || fail "--path-prefix 값이 필요합니다."
      path_prefix="$2"
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
  fail "이 스크립트는 Runtime env 파일 권한 보호를 위해 root로 실행해야 합니다."
fi

case "${scope}" in
  app|worker)
    ;;
  *)
    fail "--scope는 app 또는 worker여야 합니다."
    ;;
esac

if [[ -z "${output_path}" ]]; then
  output_path="/run/yeodam/${scope}.env"
fi

[[ "${path_prefix}" == /* ]] || fail "--path-prefix는 /로 시작해야 합니다."
path_prefix="${path_prefix%/}"

for command_name in aws dirname install mktemp python3 rm; do
  command -v "${command_name}" >/dev/null 2>&1 \
    || fail "필요한 명령을 찾을 수 없습니다: ${command_name}"
done

output_directory="$(dirname -- "${output_path}")"
install -d --owner=root --group=root --mode=0700 "${output_directory}"

temporary_directory="$(mktemp -d "${output_directory}/.runtime-env.XXXXXX")"
parameters_json="${temporary_directory}/parameters.json"
rendered_env="${temporary_directory}/${scope}.env"
parameter_path="${path_prefix}/${scope}"

aws ssm get-parameters-by-path \
  --region "${aws_region}" \
  --path "${parameter_path}" \
  --with-decryption \
  --output json \
  --no-cli-pager \
  > "${parameters_json}"

python3 - \
  "${scope}" \
  "${parameter_path}" \
  "${parameters_json}" \
  "${rendered_env}" <<'PY'
import json
import sys
from pathlib import Path

scope, parameter_path, source_path, output_path = sys.argv[1:]

schemas = {
    "app": {
        "MYSQL_URL": ("String", True, None),
        "MYSQL_USERNAME": ("String", True, None),
        "MYSQL_PASSWORD": ("SecureString", True, None),
        "AWS_REGION": ("String", True, None),
        "ATTACHMENT_S3_BUCKET": ("String", True, None),
        "AI_SERVER_BASE_URL": ("String", True, None),
        "AI_SERVER_API_KEY": ("SecureString", True, None),
        "AI_EC2_INSTANCE_ID": ("String", True, None),
        "FRONTEND_OAUTH_CALLBACK_URI": ("String", True, None),
        "KAKAO_CLIENT_ID": ("String", True, None),
        "KAKAO_REDIRECT_URI": ("String", True, None),
        "KAKAO_CLIENT_SECRET": ("SecureString", True, None),
        "JWT_SECRET": ("SecureString", True, None),
        "JWT_ISSUER": ("String", True, None),
        "FRONTEND_ORIGIN": ("String", True, None),
        "DATA_GO_KR_SERVICE_KEY": ("SecureString", False, ""),
        "DATA_GO_KR_LEGAL_DISTRICT_API_URL": ("String", True, None),
        "DATA_GO_KR_TIMEOUT": ("String", False, "10s"),
    },
    "worker": {
        "API_KEY": ("SecureString", True, None),
        "S3_BUCKET": ("String", True, None),
        "FAKE_PIPELINE": ("String", False, "0"),
        "LOG_LEVEL": ("String", False, "INFO"),
    },
}

schema = schemas[scope]
document = json.loads(Path(source_path).read_text(encoding="utf-8"))
parameters = {}

for parameter in document.get("Parameters", []):
    name = parameter.get("Name", "")
    expected_prefix = f"{parameter_path}/"
    if not name.startswith(expected_prefix):
        raise SystemExit(f"unexpected parameter path: {name}")

    key = name.removeprefix(expected_prefix)
    if "/" in key or key not in schema:
        raise SystemExit(f"unexpected parameter name: {name}")
    if key in parameters:
        raise SystemExit(f"duplicate parameter: {name}")

    expected_type = schema[key][0]
    actual_type = parameter.get("Type")
    if actual_type != expected_type:
        raise SystemExit(
            f"invalid parameter type for {name}: expected={expected_type}, actual={actual_type}"
        )

    parameters[key] = parameter.get("Value", "")

missing = [
    key
    for key, (_, required, _) in schema.items()
    if required and (key not in parameters or parameters[key] == "")
]
if missing:
    raise SystemExit("missing required parameters: " + ", ".join(missing))


def dotenv_literal(value: str) -> str:
    if "\x00" in value or "\n" in value or "\r" in value:
        raise SystemExit("parameter values must be single-line text")
    return "'" + value.replace("'", "\\'") + "'"


lines = []
for key, (_, _, default) in schema.items():
    value = parameters.get(key, default)
    lines.append(f"{key}={dotenv_literal(value)}")

Path(output_path).write_text("\n".join(lines) + "\n", encoding="utf-8")
PY

install \
  --owner=root \
  --group=root \
  --mode=0600 \
  "${rendered_env}" \
  "${output_path}"

echo "Runtime env 생성 완료: scope=${scope}, output=${output_path}"
echo "Parameter 값은 출력하지 않았습니다."
