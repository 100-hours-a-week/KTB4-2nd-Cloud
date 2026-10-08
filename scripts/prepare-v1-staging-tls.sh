#!/usr/bin/env bash
set -Eeuo pipefail

readonly DOMAIN='v1-staging.yeodam-2gether.com'
contact_email=''

while [[ $# -gt 0 ]]; do
  case "$1" in
    --email) contact_email="${2:?missing email}"; shift 2 ;;
    *) echo "Usage: sudo $0 --email CONTACT_EMAIL" >&2; exit 1 ;;
  esac
done

[[ "${EUID}" -eq 0 ]] || { echo 'Run as root.' >&2; exit 1; }
[[ "${contact_email}" == *@* ]] || { echo 'A certificate contact email is required.' >&2; exit 1; }

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install --yes --no-install-recommends certbot
install -d -m 0755 /var/www/certbot

cat > /etc/nginx/sites-available/yeodam-v1-staging-acme <<EOF
server {
    listen 80;
    server_name ${DOMAIN};
    location /.well-known/acme-challenge/ {
        root /var/www/certbot;
    }
    location / {
        return 503 "V1 staging source is not deployed yet.\\n";
    }
}
EOF

ln -sfn /etc/nginx/sites-available/yeodam-v1-staging-acme /etc/nginx/sites-enabled/yeodam-v1-staging-acme
rm -f /etc/nginx/sites-enabled/yeodam-maintenance
nginx -t
systemctl reload nginx

certbot certonly \
  --webroot \
  --webroot-path /var/www/certbot \
  --domain "${DOMAIN}" \
  --email "${contact_email}" \
  --agree-tos \
  --non-interactive

install -d -m 0755 /etc/letsencrypt/renewal-hooks/deploy
cat > /etc/letsencrypt/renewal-hooks/deploy/yeodam-v1-staging-nginx <<'EOF'
#!/usr/bin/env bash
set -Eeuo pipefail
nginx -t
systemctl reload nginx
EOF
chmod 0755 /etc/letsencrypt/renewal-hooks/deploy/yeodam-v1-staging-nginx

test -s "/etc/letsencrypt/live/${DOMAIN}/fullchain.pem"
echo "TLS certificate is ready for ${DOMAIN}."
