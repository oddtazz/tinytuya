#!/usr/bin/env python3
"""Poll a Tuya air-quality monitor DIRECTLY over the LAN and write dps to InfluxDB.

Replaces the cloud poller (deployed Aug 2026, git 46f2652). Data flows:
device --UDP/TCP LAN--> this poller --> InfluxDB. The Tuya cloud is contacted
ONLY for local-key maintenance: (a) when no key is configured at startup, or
(b) when the configured key is rejected with error 914 (= the device rotated
its local_key — the failure mode that killed the original LAN poller).
A "CLOUD CONTACT" warning is logged every time that happens. No sensor
readings ever transit Tuya's servers.

Schema is unchanged from the previous pollers, so the Grafana "Home AQI"
dashboard keeps working without edits:
  measurement: tuya_device_{numeric|enum|boolean|string}
  tags:        device_id, dps_key
  field:       value

The device is battery-powered and sleeps; when asleep it does not answer
(short connection_timeout -> error), which is expected and skipped. Pair with
a DHCP reservation so the IP never moves (MAC f8:17:2d:7b:3c:bd). A UDP
broadcast rediscovery fallback is NOT viable from a Docker bridge network
(broadcasts are not forwarded), so the IP must be configured.

Config precedence: environment variable > config.yaml (docker/config.yaml).
Env vars: TUYA_DEVICE_ID, TUYA_DEVICE_IP, TUYA_LOCAL_KEY (optional; fetched
          from cloud on 914/absent if TUYA_API_KEY+TUYA_API_SECRET are set),
          TUYA_DEVICE_VERSION (default 3.5), TUYA_API_KEY, TUYA_API_SECRET,
          TUYA_REGION (default "in"), INFLUX_URL, INFLUX_TOKEN, INFLUX_ORG,
          INFLUX_BUCKET, POLL_INTERVAL (seconds, default 60),
          STATUS_TIMEOUT (seconds, default 6), LOG_LEVEL (default INFO).
"""
import logging
import os
import sys
import time

import tinytuya

from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

# Authoritative dps -> type map (from the Tuya cloud device spec, tuya-raw.json).
# LAN status() returns dps keyed by NUMBER, so no code-name mapping is needed.
DPS_TYPES = {
    "1": "enum", "2": "numeric", "3": "numeric", "4": "numeric", "5": "numeric",
    "7": "numeric", "8": "numeric", "9": "numeric", "22": "numeric", "23": "boolean",
    "28": "enum", "101": "numeric", "102": "numeric", "103": "enum", "104": "numeric",
    "105": "numeric", "106": "boolean", "107": "numeric", "108": "numeric",
    "109": "numeric", "110": "numeric", "111": "numeric", "112": "enum",
    "113": "numeric", "114": "numeric", "115": "numeric",
    "116": "boolean", "117": "boolean", "118": "boolean",
}


def load_config():
    try:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")
        with open(path) as f:
            return yaml_load(f) or {}
    except Exception:
        return {}


def yaml_load(f):
    import yaml
    return yaml.safe_load(f)


def get_cfg(cfg, dotted, env=None, default=None):
    """Env var first, then dotted path into config.yaml, then default."""
    if env:
        val = os.environ.get(env)
        if val is not None and val != "":
            return val
    node = cfg
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def write_points(write_api, bucket, org, device_id, dps, logger):
    """Transform dps dict into InfluxDB points with the legacy schema."""
    points = []
    for key, value in dps.items():
        dtype = DPS_TYPES.get(str(key))
        if dtype is None:
            if isinstance(value, bool):
                dtype = "boolean"
            elif isinstance(value, (int, float)):
                dtype = "numeric"
            else:
                dtype = "string"
        try:
            if dtype == "numeric":
                value = float(value)
            elif dtype == "boolean":
                value = bool(value)
            else:
                value = str(value)
        except (TypeError, ValueError):
            value = str(value)
        points.append(
            Point(f"tuya_device_{dtype}")
            .tag("device_id", device_id)
            .tag("dps_key", str(key))
            .field("value", value)
        )
    write_api.write(bucket=bucket, org=org, record=points)
    logger.info("Wrote %d dps points (keys: %s)", len(points), sorted(dps.keys()))


def main():
    cfg = load_config()

    logging.basicConfig(
        level=getattr(logging, str(get_cfg(cfg, "logging.level", "LOG_LEVEL", "INFO")).upper()),
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=[logging.StreamHandler()],
    )
    logger = logging.getLogger("tuya-influx")

    influx_url = get_cfg(cfg, "influxdb.url", "INFLUX_URL", "http://10.100.10.225:8086")
    influx_token = get_cfg(cfg, "influxdb.token", "INFLUX_TOKEN")
    influx_org = get_cfg(cfg, "influxdb.org", "INFLUX_ORG", "homelab")
    influx_bucket = get_cfg(cfg, "influxdb.bucket", "INFLUX_BUCKET", "air_quality")
    try:
        poll_interval = int(get_cfg(cfg, "general.polling_interval", "POLL_INTERVAL", 60))
    except (TypeError, ValueError):
        poll_interval = 60
    try:
        status_timeout = int(get_cfg(cfg, "general.status_timeout", "STATUS_TIMEOUT", 6))
    except (TypeError, ValueError):
        status_timeout = 6

    device_id = get_cfg(cfg, "tuya.device_id", "TUYA_DEVICE_ID")
    device_ip = get_cfg(cfg, "tuya.ip_address", "TUYA_DEVICE_IP") or None
    local_key = get_cfg(cfg, "tuya.local_key", "TUYA_LOCAL_KEY") or None
    try:
        version = float(get_cfg(cfg, "tuya.version", "TUYA_DEVICE_VERSION", 3.5))
    except (TypeError, ValueError):
        version = 3.5

    api_key = get_cfg(cfg, "tuya.api_key", "TUYA_API_KEY")
    api_secret = get_cfg(cfg, "tuya.api_secret", "TUYA_API_SECRET")
    api_region = get_cfg(cfg, "tuya.region", "TUYA_REGION", "in")

    if not influx_token or not device_id:
        logger.error("Missing config: need INFLUX_TOKEN + TUYA_DEVICE_ID (env or config.yaml).")
        sys.exit(1)
    if not device_ip:
        logger.error("Missing TUYA_DEVICE_IP. The device IP must be configured "
                     "(DHCP reservation recommended); LAN UDP broadcast discovery "
                     "does not work from a Docker bridge network.")
        sys.exit(1)

    write_client = InfluxDBClient(url=influx_url, token=influx_token, org=influx_org)
    write_api = write_client.write_api(write_options=SYNCHRONOUS)
    logger.info("InfluxDB client configured for %s bucket=%s org=%s", influx_url, influx_bucket, influx_org)

    # Cloud client used ONLY for key maintenance (914 recovery / missing key).
    cloud = None
    if api_key and api_secret:
        cloud = tinytuya.Cloud(apiRegion=api_region, apiKey=api_key, apiSecret=api_secret,
                               apiDeviceID=device_id)
        logger.info("Tuya cloud client ready (key-maintenance only; region=%s)", api_region)
    else:
        logger.warning("No TUYA_API_KEY/TUYA_API_SECRET configured — local_key rotation "
                       "recovery (error 914) will NOT work until creds are added.")

    if device_ip:
        logger.info("LAN poller target: %s:%s (v%s) device %s", device_ip, 6668, version, device_id)
    else:
        logger.error("No device IP configured; exiting.")
        sys.exit(1)

    def cloud_fetch_key():
        """Contact the Tuya cloud ONCE to fetch the device's current local_key."""
        if cloud is None:
            return None
        logger.warning("CLOUD CONTACT (key maintenance only — no sensor data): fetching local_key")
        try:
            for dev in cloud.getdevices():
                if dev.get("id") == device_id and dev.get("key"):
                    logger.warning("Cloud returned a fresh local_key for %s", device_id)
                    return dev["key"]
        except Exception as e:
            logger.error("Cloud key fetch failed: %s", e)
        return None

    def poll_once():
        """One LAN status query. Returns (dps, err): dps dict on success,
        otherwise (None, err) where err '914' means the key was rejected."""
        d = tinytuya.Device(device_id, device_ip, local_key or "",
                            version=version, connection_timeout=status_timeout)
        try:
            data = d.status(nowait=False)
        except Exception as e:
            return None, f"exception={e}"
        if not isinstance(data, dict):
            return None, f"unexpected response={data!r}"
        if data.get("Err") == "914":
            return None, "914"
        if isinstance(data.get("dps"), dict) and data["dps"]:
            return data["dps"], None
        return None, str(data.get("Error") or data.get("Err") or data)[:160]

    consecutive_failures = 0
    while True:
        try:
            # Key missing at startup (first boot): fetch once from the cloud.
            if not local_key:
                new_key = cloud_fetch_key()
                if new_key:
                    local_key = new_key
                else:
                    logger.error("No local_key available; sleeping %ds and retrying", poll_interval)
                    time.sleep(poll_interval)
                    continue

            dps, err = poll_once()

            if err == "914":
                consecutive_failures += 1
                logger.warning("LAN query rejected the local_key (error 914 — key rotated)")
                new_key = cloud_fetch_key()
                if new_key and new_key != local_key:
                    local_key = new_key
                    logger.warning("Refreshed local_key; retrying immediately")
                    dps, err = poll_once()  # single retry with the fresh key
                else:
                    logger.error("Key refresh did not yield a new key; will retry next cycle")

            if err is not None and err not in ("914",):
                # Expected for a sleeping battery device; don't spam the logs.
                consecutive_failures += 1
                if consecutive_failures in (1, 10, 30) or consecutive_failures % 60 == 0:
                    logger.warning("Device not answering (asleep/unreachable) [%d in a row]: %s",
                                   consecutive_failures, err)

            if err is None and isinstance(dps, dict) and dps:
                consecutive_failures = 0
                write_points(write_api, influx_bucket, influx_org, device_id, dps, logger)

        except Exception as e:
            logger.error("Poll/write failed: %s", e)

        try:
            time.sleep(poll_interval)
        except KeyboardInterrupt:
            logger.info("Terminated by user")
            write_client.close()
            sys.exit(0)
        except Exception as e:
            logger.error("Unexpected error: %s", e)


if __name__ == "__main__":
    main()