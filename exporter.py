"""
Tesla Fleet Telemetry → Prometheus exporter.

Spawns fleet-telemetry as a subprocess, reads its stdout (JSON lines from the
'logger' dispatcher), and translates each telemetry record into Prometheus
metrics. Also watches the configured TLS cert + key for mtime changes and
restarts the subprocess on rotation so the server picks up renewed certs.

Designed to run as the container PID 1 (after entrypoint.sh).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from typing import Any

from prometheus_client import Counter, Gauge, REGISTRY, start_http_server


log = logging.getLogger("exporter")

# Lazy gauges/counters keyed by (metric_name, frozenset(labels))
# We use one Gauge per (field, vehicle_id) effectively, by maintaining a
# single Gauge per field-name with label `vehicle_id`.
_gauges: dict[str, Gauge] = {}
_counters: dict[str, Counter] = {}

records_total = Counter(
    "tesla_fleet_records_total",
    "Total number of telemetry records processed from fleet-telemetry stdout",
    ["topic"],
)
parse_errors_total = Counter(
    "tesla_fleet_parse_errors_total",
    "Lines from fleet-telemetry that could not be parsed",
)


def _sanitize(name: str) -> str:
    """Convert Tesla's CamelCase or DotName fields into snake_case Prom names."""
    out = []
    for c in name:
        if c.isupper():
            out.append("_")
            out.append(c.lower())
        elif c == ".":
            out.append("_")
        else:
            out.append(c)
    s = "".join(out).lstrip("_")
    # Replace any other non-allowed chars
    s = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in s)
    return s


def _get_or_create_gauge(field: str) -> Gauge:
    key = field
    if key not in _gauges:
        _gauges[key] = Gauge(
            f"tesla_{_sanitize(field)}",
            f"Tesla telemetry field {field}",
            ["vehicle_id"],
        )
    return _gauges[key]


def _record_value(field: str, vehicle_id: str, value: Any) -> None:
    if value is None:
        return
    if isinstance(value, bool):
        v = 1.0 if value else 0.0
    elif isinstance(value, (int, float)):
        v = float(value)
    else:
        # Tesla sometimes sends enums as strings; emit a stateset-style 1.0
        # under a label that encodes the string value.
        # Keep this simple — skip non-numeric.
        return
    _get_or_create_gauge(field).labels(vehicle_id=vehicle_id).set(v)


def handle_record(rec: dict) -> None:
    """
    Translate one fleet-telemetry logrus-JSON record into Prometheus updates.

    fleet-telemetry with `json_log_enable: true` emits lines like:
      {
        "level": "info",
        "msg": "record_payload",
        "time": "...",
        "vin": "5YJ...",
        "data": {"BatteryLevel": 79.4, "VehicleSpeed": 23, "Location": {"latitude": .., "longitude": ..}, ...},
        "metadata": {"txtype": "V", "device_client_version": "1.2.0", ...}
      }

    Other log lines (startup, handshake events, etc.) are also JSON but with
    different msg fields; we filter to msg=record_payload only.
    """
    # Only translate actual record_payload entries
    if rec.get("msg") != "record_payload":
        return

    vehicle_id = rec.get("vin") or "unknown"
    topic = (rec.get("metadata") or {}).get("txtype", "unknown")
    records_total.labels(topic=topic).inc()

    data = rec.get("data")
    if not isinstance(data, dict):
        # alerts/errors payloads come as lists of dicts; treat each like a sub-record
        if isinstance(data, list):
            for entry in data:
                if isinstance(entry, dict):
                    for k, v in entry.items():
                        _record_value(k, vehicle_id, v)
        return

    for field, value in data.items():
        # Skip metadata-ish fields
        if field in ("Vin", "CreatedAt", "IsResend", "ConnectionID", "NetworkInterface", "Status", "Name", "Audiences", "StartedAt", "EndedAt"):
            continue
        # Nested objects (e.g. Location: {latitude, longitude})
        if isinstance(value, dict):
            for sub, sub_v in value.items():
                _record_value(f"{field}_{sub}", vehicle_id, sub_v)
            continue
        _record_value(field, vehicle_id, value)


# ---------------------------------------------------------------------------
# Subprocess supervisor


def _watch_certs(paths: list[str], on_change) -> None:
    """Poll mtimes; invoke on_change when any change is detected."""
    last = {p: 0.0 for p in paths}
    for p in paths:
        try:
            last[p] = os.path.getmtime(os.path.realpath(p))
        except FileNotFoundError:
            log.warning("cert path not present yet: %s", p)

    while True:
        time.sleep(10)
        for p in paths:
            try:
                m = os.path.getmtime(os.path.realpath(p))
            except FileNotFoundError:
                continue
            if m != last[p]:
                log.info("cert change detected on %s (mtime %s → %s)", p, last[p], m)
                last[p] = m
                on_change()
                # Don't spam — after one change, sleep extra
                time.sleep(15)


def _load_cert_paths(config_path: str) -> list[str]:
    with open(config_path) as f:
        cfg = json.load(f)
    tls = cfg.get("tls", {}) or {}
    paths = []
    for k in ("server_cert", "server_key", "ca"):
        v = tls.get(k)
        if v:
            paths.append(v)
    return paths


def run(config_path: str, prom_port: int) -> int:
    start_http_server(prom_port)
    log.info("prometheus /metrics live on :%d", prom_port)

    cert_paths = _load_cert_paths(config_path)
    log.info("watching for cert mtime changes: %s", cert_paths)

    stop = threading.Event()
    current_proc: dict[str, subprocess.Popen | None] = {"p": None}

    def start_subprocess() -> subprocess.Popen:
        log.info("spawning fleet-telemetry with config %s", config_path)
        # fleet-telemetry (logrus) writes to STDERR by default. Merge it
        # into stdout so we can parse the JSON record stream from one pipe.
        return subprocess.Popen(
            ["/usr/local/bin/fleet-telemetry", "-config", config_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
            text=True,
        )

    def restart_subprocess() -> None:
        p = current_proc["p"]
        if p and p.poll() is None:
            log.info("sending SIGTERM to fleet-telemetry (pid %d) for cert reload", p.pid)
            try:
                p.terminate()
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait(timeout=5)

    # Start the cert watcher in the background
    threading.Thread(
        target=_watch_certs,
        args=(cert_paths, restart_subprocess),
        daemon=True,
    ).start()

    # Forward SIGTERM/SIGINT to the subprocess
    def _shutdown(signum, frame):
        log.info("received signal %d, shutting down", signum)
        stop.set()
        p = current_proc["p"]
        if p and p.poll() is None:
            p.terminate()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    backoff = 1.0
    while not stop.is_set():
        proc = start_subprocess()
        current_proc["p"] = proc
        backoff = 1.0
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.rstrip("\n")
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    # Not a JSON line — fleet-telemetry's own structured logs come through here too
                    sys.stderr.write(line + "\n")
                    continue
                try:
                    handle_record(rec)
                except Exception:
                    parse_errors_total.inc()
                    log.exception("error handling record")
        except KeyboardInterrupt:
            stop.set()
        finally:
            rc = proc.wait()
            log.warning("fleet-telemetry exited rc=%d", rc)

        if stop.is_set():
            break
        log.info("respawning fleet-telemetry in %.1fs", backoff)
        time.sleep(backoff)
        backoff = min(backoff * 2.0, 30.0)

    log.info("exporter exiting")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, help="Path to fleet-telemetry JSON config")
    parser.add_argument("--prom-port", type=int, default=9200)
    parser.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "info"))
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    return run(args.config, args.prom_port)


if __name__ == "__main__":
    sys.exit(main())
