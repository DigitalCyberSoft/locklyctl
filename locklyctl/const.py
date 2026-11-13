"""Constants for locklyctl.

Request shapes and the common body mirror the Lockly app (3.2.9/3.3.4) and
Forcky's LocklyHA. See LICENSE.LocklyHA and the docs fork for provenance.
"""
from __future__ import annotations

API_BASE = "https://apiserv03c.lockly.com/pgsmtlkv2/api/"
API_USER_AGENT = "DoorLocker/329 (Pixel 8 Pro; Android 16)"
# Lockly's event log, which Wi-Fi Visages upload their events to (PgConfig.j()
# and ApiService "v1/proto/handler", app 3.3.4).
EVENT_LOG_BASE = "https://apiserv04c.lockly.com/mms/api/"

# Sent on every request except `login` (COMMON_BODY in the app).
COMMON_BODY = {
    "appType": "LOCKLY",
    "ctry": "",
    "dvid": "",
    "locale": "EN",
    "os": "android",
    "rid1": "",
    "rid2": "",
    "tk": "",
    "ver": "329",
    "versionName": "3.2.9",
}

HTTP_TIMEOUT = 30          # seconds, per REST call
SENDDATA_TIMEOUT = 30      # seconds, senddata relay

# MQTT device channel (Lockly's AWS broker; client-cert auth).
MQTT_BROKER = "mqttuswest02-lb-001-b5ed8c5e37b3a497.elb.us-west-2.amazonaws.com"
MQTT_PORT = 8883
MQTT_PUBLISH_TOPIC = "server"
MQTT_CLIENT_TOPIC_PREFIX = "client/"
MQTT_RESPONSE_TIMEOUT = 20  # seconds to wait for a lockCommandResponse

MATTER_HUB_PREFIX = "PGH260"

CONFIG_DIR_NAME = ".locklyctl"
CONFIG_FILE_NAME = "config.json"
