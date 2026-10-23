#!/usr/bin/env bash
# Run only on the V1 staging App. Does not force rotation or restart Nginx.
set -Eeuo pipefail
readonly target=/etc/logrotate.d/yeodam-nginx-json
readonly backups=/var/backups/yeodam/nginx-json-logrotate
readonly expected_instance=i-05d320e3a87200d5a
readonly source_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
[[ ${EUID} -eq 0 ]] || { echo 'Run with sudo' >&2; exit 1; }
[[ $(cat /var/lib/cloud/data/instance-id) == "$expected_instance" ]] || { echo 'Wrong instance' >&2; exit 1; }
mode=${1:-install}
restore() {
    local backup=$1
    if [[ -f "$backup/previous" ]]; then
        cp -p -- "$backup/previous" "$target"
    elif [[ -f "$backup/absent" ]]; then
        rm -f -- "$target"
    else
        echo 'Invalid backup' >&2; return 1
    fi
}
case "$mode" in
    install)
        [[ $# -le 1 ]] || exit 2
        [[ ! -L "$target" ]] || { echo 'Refuse symlink target' >&2; exit 1; }
        systemctl is-active --quiet nginx
        systemctl is-active --quiet logrotate.timer
        # Debug mode performs no rotation and does not update state.
        logrotate --debug --state /dev/null "$source_dir/nginx-json.logrotate"
        if [[ -f "$target" ]] && cmp -s "$source_dir/nginx-json.logrotate" "$target"; then
            echo 'Already installed'; exit 0
        fi
        install -d -m 0700 "$backups"
        backup=$(mktemp -d "$backups/$(date -u +%Y%m%dT%H%M%SZ).XXXXXX")
        if [[ -e "$target" ]]; then cp -p -- "$target" "$backup/previous"; else touch "$backup/absent"; fi
        candidate=$(mktemp /etc/logrotate.d/.yeodam-json.XXXXXX)
        trap 'rm -f -- "${candidate:-}"' EXIT
        install -m 0644 "$source_dir/nginx-json.logrotate" "$candidate"
        mv -T -- "$candidate" "$target"
        if ! logrotate --debug /etc/logrotate.conf; then
            restore "$backup"
            echo 'Validation failed; previous configuration restored' >&2; exit 1
        fi
        echo "Installed. Rollback: sudo $source_dir/install-nginx-logrotate.sh rollback $backup"
        ;;
    rollback)
        [[ $# -eq 2 ]] || exit 2
        backup=$(realpath -e -- "$2")
        [[ "$backup" == "$backups/"* ]] || { echo 'Wrong backup directory' >&2; exit 1; }
        restore "$backup"
        logrotate --debug /etc/logrotate.conf
        echo 'Configuration restored; rotated logs and state preserved'
        ;;
    *) echo 'Usage: install-nginx-logrotate.sh [install | rollback BACKUP_DIRECTORY]' >&2; exit 2 ;;
esac
