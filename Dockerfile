# Multi-stage: copy the official fleet-telemetry binary into a slim Python base
# so we can run both the Go server and our Python exporter in one container.

ARG FLEET_TELEM_VERSION=latest

FROM tesla/fleet-telemetry:${FLEET_TELEM_VERSION} AS upstream

FROM python:3.13-slim

# fleet-telemetry binary location varies — adjust if upstream image changes layout
COPY --from=upstream /fleet-telemetry /usr/local/bin/fleet-telemetry

# Some upstream images put it in /usr/local/bin/ instead; entrypoint resolves via $PATH
RUN if [ ! -x /usr/local/bin/fleet-telemetry ]; then \
      find / -name 'fleet-telemetry' -type f -executable 2>/dev/null | head -1 | xargs -I{} cp {} /usr/local/bin/fleet-telemetry; \
    fi && chmod +x /usr/local/bin/fleet-telemetry

RUN pip install --no-cache-dir prometheus_client

WORKDIR /app
COPY entrypoint.sh /app/entrypoint.sh
COPY exporter.py   /app/exporter.py
COPY config.json.tmpl /app/config.json.tmpl
COPY tesla-prod-ca.crt /etc/tesla/prod_ca.crt
RUN chmod +x /app/entrypoint.sh

ENV TELEM_HOST=0.0.0.0 \
    TELEM_PORT=443 \
    PROM_PORT=9200 \
    NAMESPACE=tesla \
    LOG_LEVEL=info \
    CONFIG_PATH=/app/config.json \
    TLS_CLIENT_CA=/etc/tesla/prod_ca.crt

EXPOSE 443/tcp 9200/tcp

ENTRYPOINT ["/app/entrypoint.sh"]
