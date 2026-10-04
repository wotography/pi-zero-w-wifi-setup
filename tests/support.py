"""Shared test setup: isolate every path the modules read at import time.

Import this module FIRST in each test file.  The daemon and the config
server read their paths and names from the environment when they are
imported, so the environment has to point into a throw-away directory
before that happens — nothing may ever touch /etc on the test machine.
"""

import atexit
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

TMP = tempfile.mkdtemp(prefix="wifi-setup-tests-")
atexit.register(shutil.rmtree, TMP, ignore_errors=True)

os.environ.update({
    "WIFI_CFG_DIR": TMP,
    "WIFI_STORE_FILE": os.path.join(TMP, "known-wifi.json"),
    "WIFI_WPA_CONF": os.path.join(TMP, "wpa_supplicant.conf"),
    "WIFI_LOG_FILE": os.path.join(TMP, "daemon.log"),
    "WIFI_SCAN_CACHE": os.path.join(TMP, "scan-cache.json"),
    "WIFI_CONNECT_STATE": os.path.join(TMP, "connect-state.json"),
    "WIFI_PID_FILE": os.path.join(TMP, "daemon.pid"),
    "WIFI_CONFIG_HOST": "127.0.0.1",
    "WIFI_CONFIG_PORT": "8901",
    "WIFI_DEVICE_NAME": "Test-Device",
    "WIFI_COUNTRY": "GB",
})
for key in ("WIFI_AP_SSID", "WIFI_SETUP_DOMAIN", "WIFI_CERT", "WIFI_KEY",
            "WIFI_CAPTIVE_PORTAL", "WIFI_AP_PASSWORD"):
    os.environ.pop(key, None)
