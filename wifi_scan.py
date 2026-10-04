#!/usr/bin/env python3
"""Shared Wi-Fi scanning helpers for the wifi-setup daemon + config server.

The Pi Zero W's Broadcom brcmfmac driver cannot reliably scan while hostapd
owns wlan0 (the setup AP).  So the daemon scans while wlan0 is still in
client/managed mode — just before the AP comes up and on an explicit
"rescan" — and caches the results to a JSON file.  The web config server
serves that cache for /api/scan, with a live scan only as a fallback.

Also home of the device-naming helpers (device name, setup-AP SSID, local
setup domain): the daemon and the config server both import this module, so
the names are derived in exactly one place.

Deliberately stdlib-only and dependency-free (same rule as the rest of the
setup tooling).
"""

import json
import os
import re
import socket
import subprocess
import time


# --------------------------------------------------------------------------
# Device naming (shared by daemon + config server)
# --------------------------------------------------------------------------
SSID_MAX_BYTES = 32          # 802.11 limit; hostnames may be up to 63 chars
AP_SSID_SUFFIX = "-Setup"


def device_name():
    """The name shown to the user: WIFI_DEVICE_NAME, else the hostname."""
    name = (os.environ.get("WIFI_DEVICE_NAME") or "").strip()
    if not name:
        try:
            name = socket.gethostname().split(".")[0].strip()
        except OSError:
            name = ""
    return name or "device"


def default_ap_ssid(name=None):
    """`<name>-Setup`, with the name cut so the SSID fits in 32 bytes."""
    name = name or device_name()
    room = SSID_MAX_BYTES - len(AP_SSID_SUFFIX.encode("utf-8"))
    head = name.encode("utf-8")[:room].decode("utf-8", "ignore").rstrip(" -")
    return (head or "device") + AP_SSID_SUFFIX


def ap_ssid():
    """Setup-AP SSID: WIFI_AP_SSID, else derived from the device name."""
    return (os.environ.get("WIFI_AP_SSID") or "").strip() or default_ap_ssid()


def setup_domain(name=None):
    """Local name for the setup page (`<name>.setup`), DNS-label safe.

    Lowercase, only a-z/0-9/'-', max 63 chars — so "Living Room" becomes
    "living-room.setup".  WIFI_SETUP_DOMAIN overrides it.
    """
    override = (os.environ.get("WIFI_SETUP_DOMAIN") or "").strip().lower()
    if override:
        return override
    label = re.sub(r"[^a-z0-9-]+", "-", (name or device_name()).lower())
    label = label.strip("-")[:63].strip("-")
    return (label or "device") + ".setup"


def run(cmd, timeout=30):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as e:
        return -1, str(e)


def scan_networks(iface):
    """Best-effort scan for nearby SSIDs on a client-mode interface.

    Returns (networks, diagnostics).  `diagnostics` is a short human-readable
    reason string describing what actually happened ('' when networks were
    found) so a 0-result scan is diagnosable from the daemon log instead of
    looking like "empty air".

    Strategy — the Pi Zero W's brcmfmac is flaky while wpa_supplicant is
    mid-association (it returns "Device or resource busy"), so:
      1. make sure the interface is up,
      2. `iw dev <iface> scan flush` up to 3 times (flush forces a fresh scan
         instead of serving the last cached result),
      3. on failure/empty fall back to wpa_cli with a longer settle time.
    """
    run(["ip", "link", "set", iface, "up"], timeout=15)
    detail = []
    for attempt in (1, 2, 3):
        if attempt > 1:
            time.sleep(2)
        rc, out = run(["iw", "dev", iface, "scan", "flush"], timeout=30)
        if rc == 0:
            networks = _parse_scan_output(out)
            if networks:
                return _dedupe_networks(networks), ""
            detail.append("iw rc=0 but 0 results (attempt %d)" % attempt)
        else:
            detail.append("iw rc=%d (attempt %d): %s"
                          % (rc, attempt, _snippet(out)))
    run(["wpa_cli", "-i", iface, "scan"])
    time.sleep(6)
    rc, out = run(["wpa_cli", "-i", iface, "scan_results"], timeout=20)
    if rc == 0:
        networks = _parse_scan_output(out)
        if networks:
            return _dedupe_networks(networks), ""
    detail.append("wpa_cli scan_results rc=%d: %s" % (rc, _snippet(out)))
    return [], "; ".join(detail) or "no scan path succeeded"


def write_cache(networks, cache_file):
    """Atomically write scan results to cache_file.  Returns True on write."""
    try:
        os.makedirs(os.path.dirname(cache_file) or ".", exist_ok=True)
        payload = {"at": int(time.time()), "networks": networks or []}
        tmp = cache_file + ".tmp"
        with open(tmp, "w") as f:
            json.dump(payload, f)
        os.rename(tmp, cache_file)
        return True
    except Exception:
        return False


def read_cache(cache_file):
    """Return the cached entry dict {at, networks}, or None when absent."""
    if not os.path.isfile(cache_file):
        return None
    try:
        with open(cache_file) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return None
        return data
    except Exception:
        return None


def _parse_scan_output(out):
    """Parse either `iw scan` or `wpa_cli scan_results` output."""
    if ((out or "").strip().splitlines() or [""])[0].startswith("BSS "):
        return _parse_iw_scan(out)
    return _parse_wpa_scan(out)


def _parse_iw_scan(out):
    """Parse `iw dev <iface> scan` output (BSS blocks with SSID:/signal: keys)."""
    networks, cur = [], None
    for raw in (out or "").splitlines():
        line = raw.strip()
        if line.startswith("BSS "):
            if cur:
                networks.append(cur)
            cur = {"bssid": line.split()[1].split("(")[0], "ssid": "",
                   "signal": None, "security": "", "channel": ""}
        elif cur is not None:
            if line.startswith("SSID:"):
                cur["ssid"] = line.split(":", 1)[1].strip()
            elif line.startswith("signal:"):
                cur["signal"] = _to_signal(line.split()[1])
            elif line.startswith("freq:"):
                cur["channel"] = line.split()[1]
            elif line.startswith(("WPA:", "RSN:", "WEP:")):
                tok = line.split(":")[0]
                if tok not in cur["security"].split():
                    cur["security"] = (cur["security"] + " " + tok).strip()
    if cur:
        networks.append(cur)
    return networks


def _parse_wpa_scan(out):
    """Parse `wpa_cli scan_results` output (bssid/freq/signal/flags/ssid rows)."""
    networks = []
    for raw in (out or "").splitlines():
        if raw.strip().startswith("bssid /"):
            continue   # header row
        parts = raw.split()
        if len(parts) < 4:
            continue
        bssid, freq, signal, flags = parts[0], parts[1], parts[2], parts[3]
        ssid = " ".join(parts[4:])   # SSIDs may contain spaces
        if not ssid.strip():
            continue
        networks.append({
            "bssid": bssid, "ssid": ssid, "signal": _to_signal(signal),
            "security": flags.replace("[ESS]", "").strip(),
            "channel": freq,
        })
    return networks


def _to_signal(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _snippet(out, n=120):
    """First n chars of a command's output on one line (for log messages)."""
    text = (out or "").replace("\n", " ").strip()
    return text[:n] or "(no output)"


def _dedupe_networks(networks):
    """Keep the first (strongest-signal) entry per BSSID/SSID."""
    seen, out = set(), []
    for n in networks:
        if not n.get("ssid"):
            continue
        key = n.get("bssid") or n["ssid"]
        if key in seen:
            continue
        seen.add(key)
        out.append(n)
    return out
