#!/usr/bin/env bash
set -Eeuo pipefail
bundle=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
export PYTHONPATH="$bundle${PYTHONPATH:+:$PYTHONPATH}"
# This wrapper itself never sources shell credentials or runs Nginx reload.
exec python3 -m request_cutover.hostops --bundle "$bundle" "$@"
