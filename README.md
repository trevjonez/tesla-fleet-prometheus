# tesla-fleet-prometheus

Single-container bundle of [Tesla's `fleet-telemetry`](https://github.com/teslamotors/fleet-telemetry) server and a Python supervisor that translates its JSON output into Prometheus metrics on `/metrics`.

## How it works

```
                                              ┌──────────────────────────┐
                                              │ tesla-fleet-prometheus    │
                                              │                           │
Tesla vehicle  ─TLS / mTLS / protobuf─►       │  fleet-telemetry (Go)     │
                                              │   stdout JSON ↓           │
                                              │  exporter.py (supervisor) │
                                              │   ├─ updates Prom gauges  │
Prometheus     ◄──── HTTP /metrics ───────    │   └─ watches cert mtimes  │
                                              │       SIGTERM on rotation │
                                              └──────────────────────────┘
```

- Base image: `tesla/fleet-telemetry` (binary copied into `python:3.13-slim`).
- The upstream Go server doesn't hot-reload TLS certs — it calls `ListenAndServeTLS` once at startup. The supervisor watches the configured cert + key + CA mtimes and SIGTERMs the subprocess on change so renewals land without manual intervention.
- All config flows in via environment variables — no extra files needed at runtime besides the TLS material (mounted in).

## Environment variables

| Variable | Required | Default | Purpose |
|----------|----------|---------|---------|
| `TELEM_HOST` | no | `0.0.0.0` | Bind interface for the telemetry server |
| `TELEM_PORT` | no | `443` | Bind port for inbound vehicle connections |
| `PROM_PORT` | no | `9200` | Bind port for `/metrics` |
| `TLS_CERT` | **yes** | — | Path inside the container to the server cert (PEM) |
| `TLS_KEY` | **yes** | — | Path inside the container to the server key (PEM) |
| `TLS_CLIENT_CA` | no | `/etc/tesla/prod_ca.crt` | Path inside the container to the CA bundle that validates vehicle (client) certs. The image ships with Tesla's production CA bundle (vendored from [teslamotors/fleet-telemetry](https://github.com/teslamotors/fleet-telemetry/blob/main/config/files/prod_ca.crt)). Override only if you need a newer / different bundle. |
| `NAMESPACE` | no | `tesla` | `namespace` field in the rendered fleet-telemetry config |
| `LOG_LEVEL` | no | `info` | `info`, `debug`, `warn`, `error` |

## Example `docker run`

```bash
docker run -d \
  --name tesla-fleet-prometheus \
  --restart unless-stopped \
  -p 443:443 -p 9200:9200 \
  -v /path/to/certs:/certs:ro \
  -e TLS_CERT=/certs/fullchain.pem \
  -e TLS_KEY=/certs/privkey.pem \
  -e TLS_CLIENT_CA=/certs/tesla-ca.pem \
  ghcr.io/trevjonez/tesla-fleet-prometheus:main
```

## Metrics

For each numeric telemetry field a Tesla vehicle reports:

```
tesla_<field_snake_case>{vehicle_id="<vin>"} <numeric value>
```

Plus housekeeping:

```
tesla_fleet_records_total{topic="V"} <count>
tesla_fleet_parse_errors_total <count>
```

Boolean fields are emitted as `1.0` / `0.0`. Enum and string fields are skipped (no Prometheus representation).

## Build locally

```bash
docker build -t tesla-fleet-prometheus:dev .
```

## Provisioning notes

- **Tesla CA bundle** — included in the image at `/etc/tesla/prod_ca.crt`. Vendored from upstream `teslamotors/fleet-telemetry`. Update the image (or override via env + mount) if Tesla rotates the bundle.
- **Public server cert** — Tesla's mTLS protocol requires a publicly-trusted server certificate; self-signed certs will not work. Let's Encrypt wildcard or per-host certs are typical.
- **Cert renewal** — if your server cert is renewed by another tool (e.g. acme.sh, certbot, or a reverse-proxy manager) and the file path you mount stays the same, the supervisor detects the mtime change within ~10 seconds and restarts fleet-telemetry to pick up the new cert. End-to-end downtime is single-digit seconds.
- **Throughput** — for a single-vehicle homelab, expect single-digit MB of telemetry per day.

### Reusing a reverse-proxy-managed cert

If you already run nginx-proxy-manager, Caddy, Traefik or similar and have a Let's Encrypt wildcard cert managed there, you can mount its data directory read-only and point the container at the live cert files. The supervisor will pick up the renewal automatically. Example with nginx-proxy-manager (cert dir layout: `live/<id>/{fullchain,privkey}.pem`):

```bash
-v /path/to/nginx-proxy-manager/letsencrypt:/etc/letsencrypt:ro \
-e TLS_CERT=/etc/letsencrypt/live/<id>/fullchain.pem \
-e TLS_KEY=/etc/letsencrypt/live/<id>/privkey.pem \
```

## Status

Alpha — not yet tested against a real telemetry stream. The Python parser assumes the upstream `logger` dispatcher emits one JSON object per line with a `topic`, `vehicle_id`, and `data: [{key, value: {<typed-value>}}, …]` shape. Real-world schema may need adjustments to `handle_record()`.

## License

MIT.
