#!/bin/sh
set -eu

python - <<'PY'
import ipaddress
import os

value = os.environ.get("FASTCLOUD_PUBLIC_IP", "")
try:
    address = ipaddress.IPv4Address(value)
except ipaddress.AddressValueError as exc:
    raise SystemExit(f"FASTCLOUD_PUBLIC_IP must be a public IPv4 address: {exc}")
if not address.is_global:
    raise SystemExit("FASTCLOUD_PUBLIC_IP must be a public IPv4 address")
PY

IP="$FASTCLOUD_PUBLIC_IP"
CERT="/etc/letsencrypt/live/$IP/fullchain.pem"
mkdir -p /var/www/acme/.well-known/acme-challenge
cp /etc/nginx/bootstrap.conf /etc/nginx/conf.d/default.conf
nginx
trap 'nginx -s quit; exit 0' TERM INT

if [ ! -s "$CERT" ]; then
    echo "Requesting a public HTTPS certificate for $IP"
    until certbot certonly --webroot --webroot-path /var/www/acme \
        --ip-address "$IP" --cert-name "$IP" --preferred-profile shortlived \
        --non-interactive --agree-tos --register-unsafely-without-email; do
        echo "Certificate issuance failed; check that public port 80 reaches this server. Retrying in 10 minutes."
        sleep 600 & wait $!
    done
fi

sed "s/@IP@/$IP/g" /etc/nginx/https.conf > /etc/nginx/conf.d/default.conf
nginx -t
nginx -s reload
echo "HTTPS ready at https://$IP"

while :; do
    if ! nginx -t; then
        echo "Nginx configuration check failed" >&2
        exit 1
    fi
    certbot renew --quiet --deploy-hook 'nginx -s reload' || echo "Certificate renewal failed; will retry in 12 hours" >&2
    sleep 43200 & wait $!
done
