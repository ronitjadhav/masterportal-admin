#!/usr/bin/env sh
# Self-signed TLS cert for the one-origin demo. The browser will warn on first
# visit (self-signed) — expected; click through for local use. For production,
# drop real cert.pem + key.pem into deploy/certs/ instead of running this.
set -e
DIR="$(cd "$(dirname "$0")" && pwd)/certs"
mkdir -p "$DIR"
openssl req -x509 -newkey rsa:2048 -nodes -days 365 \
  -keyout "$DIR/key.pem" -out "$DIR/cert.pem" \
  -subj "/CN=localhost" \
  -addext "subjectAltName=DNS:localhost,IP:127.0.0.1"
echo "wrote $DIR/cert.pem and $DIR/key.pem"
