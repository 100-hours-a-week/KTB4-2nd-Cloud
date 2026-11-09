#!/bin/sh
set -eu
umask 077
# Passwords are generated hex and never written to the PVC or printed.
hash_password() {
  value=$(cat "/credentials/$1-password")
  case "$value" in *[!a-f0-9]*|'') exit 1;; esac
  [ "${#value}" -eq 64 ] || exit 1
  printf '%s' "$value" | sha256sum | cut -d' ' -f1
}
mkdir -p /data/files
{
  # Redis7 AOF EXEC replay uses the disabled default user; keep replay rights.
  printf 'user default reset off ~* &* +@all\n'
  printf 'user admin on #%s ~* &* +@all\n' "$(hash_password admin)"
  printf 'user app on #%s ~* &* +@connection +@read +@write +@transaction +@scripting +@pubsub -@admin -@dangerous +info +time\n' "$(hash_password app)"
  printf 'user health on #%s +ping +info\n' "$(hash_password health)"
} > /tmp/users.acl
exec redis-server /runtime/redis.conf
