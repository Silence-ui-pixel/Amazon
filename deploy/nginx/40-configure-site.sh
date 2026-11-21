#!/bin/sh
set -eu

http_template=/opt/keyword-nginx/http.conf.template
https_template=/opt/keyword-nginx/https.conf.template
certificate="/etc/letsencrypt/live/${SERVER_NAME}/fullchain.pem"
private_key="/etc/letsencrypt/live/${SERVER_NAME}/privkey.pem"

if [ -s "$certificate" ] && [ -s "$private_key" ]; then
    template=$https_template
elif [ "${REQUIRE_TLS:-0}" = "1" ]; then
    echo "TLS is required but certificate files are unreadable" >&2
    exit 1
else
    template=$http_template
fi

envsubst '${BACKEND_UPSTREAM} ${SERVER_NAME}' < "$template" > /etc/nginx/conf.d/default.conf
