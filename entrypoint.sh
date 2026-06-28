#!/bin/sh
set -eu

: "${TELEM_HOST:?required}"
: "${TELEM_PORT:?required}"
: "${PROM_PORT:?required}"
: "${TLS_CERT:?required}"
: "${TLS_KEY:?required}"
: "${TLS_CLIENT_CA:?required}"
: "${NAMESPACE:=tesla}"
: "${LOG_LEVEL:=info}"
: "${CONFIG_PATH:=/app/config.json}"

# Render config from template using envsubst-style sed (avoids the envsubst dep)
sed \
  -e "s|\${TELEM_HOST}|${TELEM_HOST}|g" \
  -e "s|\${TELEM_PORT}|${TELEM_PORT}|g" \
  -e "s|\${LOG_LEVEL}|${LOG_LEVEL}|g" \
  -e "s|\${NAMESPACE}|${NAMESPACE}|g" \
  -e "s|\${TLS_CERT}|${TLS_CERT}|g" \
  -e "s|\${TLS_KEY}|${TLS_KEY}|g" \
  -e "s|\${TLS_CLIENT_CA}|${TLS_CLIENT_CA}|g" \
  /app/config.json.tmpl > "$CONFIG_PATH"

echo "=== rendered config $CONFIG_PATH ==="
cat "$CONFIG_PATH" >&2

# Verify cert+key readable
for f in "$TLS_CERT" "$TLS_KEY" "$TLS_CLIENT_CA"; do
  [ -r "$f" ] || { echo "ERROR: cannot read $f" >&2; exit 1; }
done

# Hand off to python supervisor
exec python3 -u /app/exporter.py --config "$CONFIG_PATH" --prom-port "$PROM_PORT"
