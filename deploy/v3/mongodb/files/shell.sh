#!/bin/bash
set -Eeuo pipefail
umask 077
mkdir -p /tmp/mongosh-logs
exec mongosh --nodb --norc --quiet --file "$1"
