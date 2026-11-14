#!/bin/bash
set -euo pipefail

APP_DIR=$(cd "$(dirname "$0")/.." && pwd)
if [[ ! -f "$APP_DIR/.env" ]]; then
  echo "Create $APP_DIR/.env from .env.example before running this script."
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y python3-venv python3-pip nginx certbot python3-certbot-nginx ca-certificates curl
if ! command -v npm >/dev/null 2>&1; then
  curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
  apt-get install -y nodejs
fi

id fossfinder >/dev/null 2>&1 || useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin fossfinder
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"
(cd "$APP_DIR/web" && npm ci && npm run build)

install -m 644 "$APP_DIR/deploy/nginx/fossfinder.work.conf" /etc/nginx/sites-available/fossfinder.work
ln -sfn /etc/nginx/sites-available/fossfinder.work /etc/nginx/sites-enabled/fossfinder.work
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl reload nginx

sed "s|/opt/fossfinder|$APP_DIR|g" "$APP_DIR/deploy/fossfinder.service" > /etc/systemd/system/fossfinder.service
chown -R fossfinder:fossfinder "$APP_DIR"
systemctl daemon-reload
systemctl enable --now fossfinder

ufw allow OpenSSH
ufw allow "Nginx Full"
ufw --force enable

if [[ -n "${CERTBOT_EMAIL:-}" ]]; then
  certbot --nginx -d fossfinder.work -d www.fossfinder.work \
    --non-interactive --agree-tos -m "$CERTBOT_EMAIL" --redirect \
    || echo "Certificate was not issued. Point Porkbun at this server, then run the script again."
else
  echo "HTTP is up. After Porkbun points at this server, run:"
  echo "  CERTBOT_EMAIL=you@example.com sudo -E bash deploy/vultr.sh"
fi
