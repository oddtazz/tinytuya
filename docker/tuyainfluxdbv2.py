#!/usr/bin/env python3
"""Poll the Tuya air-quality monitor via the Tuya CLOUD API and write dps to InfluxDB.

Replaces the LAN poller (tuyainfluxdbv2_lan.py). Cloud polling is immune to the
device's battery sleep cycles and DHCP IP churn, and returns ALL 29 dps.

Schema is identical to the old poller so the Grafana "Home AQI" dashboard keeps
working unchanged:
  measurement: tuya_device_{numeric|enum|boolean|string}
  tags:        device_id, dps_key
  field:       value

Config precedence: environment variable > config.yaml (docker/config.yaml).
Env vars: TUYA_API_KEY, TUYA_API_SECRET, TUYA_REGION, TUYA_DEVICE_ID,
          INFLUX_URL, INFLUX_TOKEN, INFLUX_ORG, INFLUX_BUCKET,
          POLL_INTERVAL (seconds, default 300), LOG_LEVEL (default INFO).
"""
import logging
import os
import sys
import time

import tinytuya
import yaml

from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

# Authoritative dps -> type map (from the Tuya cloud device spec, tuya-raw.json)
DPS_TYPES = {
    "1": "enum", "2": "numeric", "3": "numeric", "4": "numeric", "5": "numeric",
    "7": "numeric", "8": "numeric", "9": "numeric", "22": "numeric", "23": "boolean",
    "28": "enum", "101": "numeric", "102": "numeric", "103": "enum", "104": "numeric",
    "105": "numeric", "106": "boolean", "107": "numeric", "108": "numeric",
    "109": "numeric", "110": "numeric", "111": "numeric", "112": "enum",
    "113": "numeric", "114": "numeric", "115": "numeric",
    "116": "boolean", "117": "boolean", "118": "boolean",
}

# The cloud API returns dps by CODE name; the Grafana dashboard queries dps NUMBERS.
CODE_TO_DPS = {
    "air_quality_index": "1", "temp_current": "2", "humidity_value": "3",
    "co2_value": "4", "ch2o_value": "5", "pm25_value": "7", "pm1": "8",
    "pm10": "9", "battery_percentage": "22", "charge_state": "23",
    "alarm_volume": "28", "tvoc_value": "101", "co_value": "102",
    "bl_level": "103", "co2_alarm_value": "104", "sleep_timer": "105",
    "buzz": "106", "pm03": "107", "secondcal": "108", "clock1": "109",
    "clock2": "110", "clock3": "111", "temp_unit_convert": "112",
    "co_alarm_value": "113", "pm25_alarm_value": "114", "hcho_alarm_value": "115",
    "clock1_en": "116", "clock2_en": "117", "clock3_en": "118",
}


def load_config():
    try:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")
        with open(path) as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


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
        poll_interval = int(get_cfg(cfg, "general.polling_interval", "POLL_INTERVAL", 300))
    except (TypeError, ValueError):
        poll_interval = 300

    api_key = get_cfg(cfg, "tuya.api_key", "TUYA_API_KEY")
    api_secret = get_cfg(cfg, "tuya.api_secret", "TUYA_API_SECRET")
    api_region = get_cfg(cfg, "tuya.region", "TUYA_REGION", "in")
    device_id = get_cfg(cfg, "tuya.device_id", "TUYA_DEVICE_ID")

    if not all([influx_token, api_key, api_secret, device_id]):
        logger.error(
            "Missing config. Need INFLUX_TOKEN + TUYA_API_KEY/TUYA_API_SECRET/TUYA_DEVICE_ID "
            "(env vars or config.yaml)."
        )
        sys.exit(1)

    # InfluxDB client (lazy auth; errors surface on first write)
    write_client = InfluxDBClient(url=influx_url, token=influx_token, org=influx_org)
    write_api = write_client.write_api(write_options=SYNCHRONOUS)
    logger.info("InfluxDB client configured for %s bucket=%s org=%s", influx_url, influx_bucket, influx_org)

    cloud = tinytuya.Cloud(apiRegion=api_region, apiKey=api_key, apiSecret=api_secret, apiDeviceID=device_id)
    logger.info("Tuya cloud client ready (region=%s device=%s)", api_region, device_id)

    while True:
        try:
            data = cloud.getstatus(device_id)
            dps = None
            if isinstance(data, dict):
                if isinstance(data.get("dps"), dict):
                    dps = data["dps"]  # LAN-style keyed dict
                elif isinstance(data.get("result"), list):
                    # Cloud format: [{"code": "temp_current", "value": 24}, ...]
                    dps = {
                        CODE_TO_DPS.get(str(item.get("code")), str(item.get("code"))): item.get("value")
                        for item in data["result"]
                        if isinstance(item, dict) and "value" in item
                    }
                elif isinstance(data.get("result"), dict) and isinstance(data["result"].get("dps"), dict):
                    dps = data["result"]["dps"]
            if not isinstance(dps, dict) or not dps:
                logger.warning("No dps in cloud status: %s", str(data)[:300])
            else:
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
                write_api.write(bucket=influx_bucket, org=influx_org, record=points)
                logger.info("Wrote %d dps points (keys: %s)", len(points), sorted(dps.keys()))
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
