#!/usr/bin/env python3
"""wifi-setup supervisor daemon.

Boot-time logic:
  1. Regenerate /etc/wpa_supplicant/wpa_supplicant.conf from the known-networks
     store (known-wifi.json) and reconfigure wlan0.
  2. Poll for connectivity for up to CONNECT_TIMEOUT seconds.  If a known
     network is reachable we log success and STAY RUNNING as a supervisor.  We
     never launch any application ourselves.
  3. Otherwise bring up the setup Access Point (hostapd + dnsmasq) and the
     HTTP(S) config server so a phone/PC can scan and save a network.

Deliberately stdlib-only: no Flask, no third-party packages.  Light enough for
a Pi Zero W.

Design notes (see docs/DESIGN.md):
  - wpa_supplicant self-heals reconnects; the supervisor only watches the
    clock and, if the link is down for > SETUP_TIMEOUT, opens the setup AP
    live (no reboot).  Because that assumption only holds while wpa_supplicant's
    network list is intact, the supervisor also performs ONE self-heal per
    outage (enable_network all + reassociate) before giving up on client mode.
  - `wpa_cli select_network <id>` (Save & connect) disables every OTHER network
    and turns auto-reconnect off.  Three independent guards keep that from
    becoming permanent: every switch ends with enable_all_networks(), the
    generated conf forces `update_config=0` so wpa_supplicant can never write
    the state to disk, and the conf is only written once no wpa_supplicant is
    left alive to save a stale config back over it.  The store is the only
    source of truth; the conf is a generated artefact.  Without these, a device
    could stay off its home WiFi indefinitely after a Save & connect
    somewhere else.
  - hostapd and wpa_supplicant cannot both own wlan0.  Entering setup mode
    stops wpa_supplicant and starts hostapd on wlan0.  The web UI's "Save &
    connect" attempts a live wpa_cli attach without rebooting; on failure it
    reports back and the user picks "Save & reboot" deliberately.
  - An OPEN (password-less) setup AP auto-powers-off the whole device after
    AP_AUTO_OFF_SECONDS (default 15 min).  A secured AP stays up until acted on.
  - dhcpcd is paused while the setup AP is up (marker file in CFG_DIR) so the
    system DHCP client can't re-DHCP wlan0 and steal/refresh the static AP
    address; it is restored on AP teardown or at the next daemon start.
  - daemon.log is written by an event-triggered, buffered, size-rotating
    handler (1 MB, one `.1` backup): WARNING+ flushes immediately, INFO within
    ~1s, empty buffers do no I/O.  The periodic flush — not atexit/SIGTERM —
    is the real safety net against power loss on the SD card.
"""

import atexit
import json
import logging
import logging.handlers
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time

import wifi_scan

# --------------------------------------------------------------------------
# Configuration (overridable via environment)
# --------------------------------------------------------------------------
def _pick_store():
    """Pick the canonical known-wifi.json store."""
    cfg = os.environ.get("WIFI_CFG_DIR", "/etc/wifi-setup")
    if os.environ.get("WIFI_STORE_FILE"):
        return os.environ["WIFI_STORE_FILE"]
    return os.path.join(cfg, "known-wifi.json")


CFG_DIR            = os.environ.get("WIFI_CFG_DIR", "/etc/wifi-setup")
# Canonical known-networks store (the web UI + daemon both read/write this).
STORE_FILE         = os.environ.get("WIFI_STORE_FILE", _pick_store())
WPA_CONF           = os.environ.get("WIFI_WPA_CONF", "/etc/wpa_supplicant/wpa_supplicant.conf")

WLAN_IFACE         = os.environ.get("WIFI_IFACE", "wlan0")

# Directory holding wpa_supplicant's per-interface control sockets.  On a stock
# Pi this appears at boot via a systemd-tmpfiles rule from the wpa_supplicant
# package; on minimal/lite images that rule can be absent, and wpa_supplicant
# only creates the per-interface SOCKET when this DIR already exists.  The
# daemon ensures it before every attach (see _ensure_wpa_ctrl_dir).
WPA_CTRL_DIR       = "/var/run/wpa_supplicant"

# Bullseye's per-interface unit (wpa_supplicant@wlan0) reads
# wpa_supplicant-wlan0.conf instead of WPA_CONF.  We keep an identical mirror
# so whichever instance actually owns wlan0 sees our networks.
WPA_IFACE_CONF     = os.path.join(os.path.dirname(WPA_CONF),
                                  "wpa_supplicant-%s.conf" % WLAN_IFACE)

# Per-network `priority=` emitted into the generated conf.  Without it
# wpa_supplicant uses its documented default: associate with the *strongest*
# BSSID among all enabled networks.  With two overlapping 2.4 GHz networks that
# is close to a coin flip (measured: -51 dBm vs -58 dBm, swinging), and losing
# it means landing on a network without the services the device needs (e.g.
# its server or broker lives on only one of them).  A store entry
# flagged `preferred` gets the high number; everything else stays low, so
# wpa_supplicant tries the favourite first and only breaks ties by signal
# *within* one priority level.  Deliberately NOT a pin: with the preferred
# network out of range or disabled, the others are still tried normally.
PREFERRED_PRIORITY = 10
DEFAULT_PRIORITY   = 1

CONNECT_TIMEOUT    = int(os.environ.get("WIFI_CONNECT_TIMEOUT", "90"))
POLL_INTERVAL      = int(os.environ.get("WIFI_POLL_INTERVAL", "5"))

# While running, if the link stays down this long we drop into setup mode so
# you can reconfigure without rebooting.  wpa_supplicant handles reconnects
# itself; we only watch the clock.
SETUP_TIMEOUT      = int(os.environ.get("WIFI_SETUP_TIMEOUT", "60"))

# A live "Save & connect" teardown the AP on purpose to try client mode.  If it
# neither connected (via the network) nor was reported as connected, bring the
# setup AP back up after this many seconds so the page the user is on survives.
AP_RESTORE_GRACE   = int(os.environ.get("WIFI_AP_RESTORE_GRACE", "45"))

# A "connecting" connect-state older than this is treated as dead (the config
# server likely died mid-switch), so the AP restore can proceed.
CONNECT_STATE_STALE = int(os.environ.get("WIFI_CONNECT_STATE_STALE", "120"))

# Consecutive failed attempts to bring the setup AP up before we stop trying
# and hand wlan0 back to wpa_supplicant.  Without an escape hatch a broken
# hostapd turns into a permanent silent limbo: no client link (the AP owns
# wlan0 / wpa_supplicant was killed) and no setup AP either.
# Giving up is NOT a dead end and needs no operator action: manual_exit_setup()
# returns into supervise(), which opens setup mode again on the next link-down
# (SETUP_TIMEOUT), so the counter simply starts over once hostapd recovers.
AP_FAIL_LIMIT      = int(os.environ.get("WIFI_AP_FAIL_LIMIT", "3"))

# Password for the SETUP AP itself.  Default: open (insecure) unless set.
#   WIFI_AP_PASSWORD="choose-a-long-one"  -> WPA2-PSK AP, only holders can join.
# An open AP (no password) auto-powers-off the device after AP_AUTO_OFF_SECONDS.
#
# Naming: the user-facing name is the hostname (WIFI_DEVICE_NAME overrides).
# The AP is "<name>-Setup" (cut to the 32-byte SSID limit; WIFI_AP_SSID
# overrides) and the page is also reachable as "<name>.setup" (DNS-label safe,
# WIFI_SETUP_DOMAIN overrides).  Derived in wifi_scan so the config server
# shows exactly the same names.
DEVICE_NAME        = wifi_scan.device_name()
AP_SSID            = wifi_scan.ap_ssid()
SETUP_DOMAIN       = wifi_scan.setup_domain()
AP_CHANNEL         = os.environ.get("WIFI_AP_CHANNEL", "6")
AP_PASSWORD        = os.environ.get("WIFI_AP_PASSWORD", "")      # empty => open AP


def ap_password_problem(password):
    """Why `password` is not a usable WPA2 passphrase, or None when it is.

    IEEE 802.11i defines the passphrase as 8-63 *printable ASCII* characters.
    hostapd refuses other lengths outright (the AP never comes up) and accepts
    non-ASCII ("€", umlauts) as raw UTF-8 bytes — but phones may refuse to
    type it or derive a different key, so joining fails with the "right"
    password.  An empty password (open AP) is not checked here.
    """
    if any(not (32 <= ord(c) <= 126) for c in password):
        return ("contains non-ASCII characters (e.g. '€' or umlauts) — many "
                "devices cannot join; use plain ASCII only")
    if not 8 <= len(password) <= 63:
        return "must be 8-63 characters long (has %d)" % len(password)
    return None
AP_AUTO_OFF_SECONDS = int(os.environ.get("WIFI_AP_AUTO_OFF_SECONDS", "900"))
AP_IP              = "192.168.4.1"
AP_SUBNET          = "192.168.4.0/24"

# Captive portal: dnsmasq answers EVERY name with AP_IP, so a phone's
# connectivity probe reaches the config server, which redirects it to the setup
# page and the phone pops up its captive-portal sheet.  "0" = only
# SETUP_DOMAIN resolves (the escape hatch if a client misbehaves).  The
# config server inherits this env and drops its probe redirect too.
CAPTIVE_PORTAL     = os.environ.get("WIFI_CAPTIVE_PORTAL", "1").strip() != "0"

# When an OPEN (password-less) setup AP is up, this is the epoch at which the
# daemon powers the whole device off.  None when the AP is secured.
SETUP_EXPIRES_AT   = None

# TLS for the config server.  If CERT/KEY are set, the config server and the
# config page use HTTPS so entered WiFi passwords are never sent in clear.
CERT_FILE          = os.environ.get("WIFI_CERT", os.path.join(CFG_DIR, "config-cert.pem"))
KEY_FILE           = os.environ.get("WIFI_KEY", os.path.join(CFG_DIR, "config-key.pem"))
CONFIG_HOST        = AP_IP   # the spawned config server binds only to the AP IP
CONFIG_PORT        = int(os.environ.get("WIFI_CONFIG_PORT", "443" if os.environ.get("WIFI_CERT") else "80"))

HOSTAPD_CONF       = "/etc/hostapd/hostapd.conf"
DNSMASQ_CONF       = "/etc/dnsmasq.d/wifi-setup.conf"

# Flat log file the web UI can tail (avoids running the server as root for
# journalctl).  Written by the daemon; read by the config server.
LOG_FILE           = os.environ.get("WIFI_LOG_FILE", os.path.join(CFG_DIR, "daemon.log"))

# PID file so we can kill stale instances WITHOUT pkill-ing ourselves.
PID_FILE           = os.environ.get("WIFI_PID_FILE", os.path.join(CFG_DIR, "daemon.pid"))

# Cross-process hint written by the config server after a SUCCESSFUL live
# ("Save & connect") wifi switch.  The daemon watches it while in setup mode
# so it can cancel the open-AP auto-poweroff and resume supervising instead.
# File-based so the separate config-server process needs no signal/IPC.
CONNECT_STATE_FILE = os.environ.get("WIFI_CONNECT_STATE",
                                    os.path.join(CFG_DIR, "connect-state.json"))

# Marker written when we pause dhcpcd for the setup AP and removed when it is
# restored.  It survives a daemon crash so the next start repairs the system
# (see main / resume_dhcpcd).
DHCPCD_PAUSE_MARKER = os.path.join(CFG_DIR, "dhcpcd-paused")

# Cached scan results written by the daemon while wlan0 is still in client
# mode and served by the config server's /api/scan (the Pi Zero W's brcmfmac
# driver cannot scan while the setup AP is up; we scan before it starts and
# refresh on an explicit "rescan").
SCAN_CACHE_FILE = os.environ.get("WIFI_SCAN_CACHE",
                                 os.path.join(CFG_DIR, "scan-cache.json"))

LOG_LEVEL          = os.environ.get("WIFI_LOG_LEVEL", "INFO").upper()


def _derive_country():
    """Best-effort regulatory country: env > existing conf > /etc/default/crda."""
    v = os.environ.get("WIFI_COUNTRY")
    if v and v.strip():
        return v.strip()[:2].upper()
    for p in (WPA_CONF, WPA_IFACE_CONF):
        try:
            with open(p) as f:
                for line in f:
                    low = line.strip().lower()
                    if low.startswith("country="):
                        c = line.split("=", 1)[1].strip().strip('"').strip("'")
                        if c:
                            return c[:2].upper()
        except Exception:
            continue
    try:
        with open("/etc/default/crda") as f:
            for line in f:
                if line.strip().startswith("REGDOMAIN="):
                    c = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if c:
                        return c[:2].upper()
    except Exception:
        pass
    return None


# Regulatory country written when we generate a fresh wpa_supplicant head.
# Nothing configured anywhere is a setup mistake, not a default: fall back so
# the radio still works, but say so loudly at start (see main()).
COUNTRY_FALLBACK = "DE"
COUNTRY = _derive_country()
COUNTRY_GUESSED = COUNTRY is None
if COUNTRY_GUESSED:
    COUNTRY = COUNTRY_FALLBACK

log = logging.getLogger("wifi_setup_daemon")


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
def run(cmd, timeout=30):
    """Run a command, swallow errors, return (returncode, stdout)."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as e:
        return -1, str(e)


def has_ipv4(iface):
    """Return the IPv4 address of iface, or None."""
    rc, out = run(["ip", "-4", "addr", "show", "dev", iface])
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("inet "):
            return line.split()[1].split("/")[0]
    return None


# --------------------------------------------------------------------------
# Connect-state file (config server -> daemon "I'm switching / I rejoined")
# --------------------------------------------------------------------------
def _write_connect_state(payload):
    try:
        os.makedirs(os.path.dirname(CONNECT_STATE_FILE) or ".", exist_ok=True)
        tmp = CONNECT_STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(payload, f)
        shutil.move(tmp, CONNECT_STATE_FILE)
    except Exception:
        log.warning("Could not write connect-state (%s)", CONNECT_STATE_FILE)


def mark_connecting(ssid):
    """Write "a live switch is in flight" so the daemon holds off AP restore."""
    _write_connect_state({"connected": False, "connecting": True,
                          "ssid": ssid, "at": int(time.time())})


def mark_connected(ssid):
    """Write "a live switch succeeded" so the daemon resumes supervising."""
    _write_connect_state({"connected": True, "ssid": ssid,
                          "at": int(time.time())})


def mark_exit_setup():
    """Ask the daemon to leave setup mode and reconnect to a saved network."""
    _write_connect_state({"exit_setup": True, "at": int(time.time())})


def mark_rescan():
    """Ask the daemon to pause the AP, rescan, and bring the AP back up."""
    _write_connect_state({"rescan": True, "at": int(time.time())})


def clear_connect_state():
    """Drop any stale connect-state (daemon boot / entering setup mode)."""
    try:
        if os.path.exists(CONNECT_STATE_FILE):
            os.remove(CONNECT_STATE_FILE)
    except OSError:
        pass


def read_connect_state():
    if not os.path.isfile(CONNECT_STATE_FILE):
        return {}
    try:
        with open(CONNECT_STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _route_via_wlan():
    """True when a default route exists through the wlan interface.

    A default route means the link actually negotiated a gateway (DHCP done),
    without depending on ICMP — some routers block pings and that would make
    an otherwise-working link look disconnected.
    """
    rc, out = run(["sh", "-c",
                   "ip route show default | awk '/%s/ {print; exit}'"
                   % WLAN_IFACE])
    return bool(out.strip())


def wpa_cli(*args):
    """wpa_cli command array pinned to our control-interface directory.

    Pointing wpa_cli at WPA_CTRL_DIR explicitly removes any dependence on the
    distro's compile-time default; the socket the daemon expects is the one at
    WPA_CTRL_DIR/<iface>, so every client call must use the same directory.
    """
    return ["wpa_cli", "-i", WLAN_IFACE, "-p", WPA_CTRL_DIR] + list(args)


def wpa_state():
    """Current wpa_supplicant state of wlan0 ("COMPLETED" when associated)."""
    rc, out = run(wpa_cli("status"), timeout=5)
    if rc != 0:
        return ""
    for line in out.splitlines():
        if line.startswith("wpa_state="):
            return line.split("=", 1)[1].strip().upper()
    return ""


def wpa_snapshot():
    """(wpa_state, ssid_or_None, ip_or_None) - cheap, for transition logging.

    The 5s poll granularity means an instantaneous blip can fall between
    samples; logging each *distinct* signature (and the moment the link stops
    reporting COMPLETED) is what makes a "connects for ~14s then dies" case
    readable in daemon.log.
    """
    state = wpa_state()
    rc, out = run(wpa_cli("status"), timeout=5)
    ssid = None
    if rc == 0:
        for line in (out or "").splitlines():
            key, _, val = line.partition("=")
            if key == "ssid" and val:
                ssid = val
                break
    return state, ssid, has_ipv4(WLAN_IFACE)


def dns_works():
    rc, out = run(["getent", "hosts", "example.com"])
    return rc == 0 and "example.com" in out


def is_connected():
    """Connectivity test that CANNOT be fooled by stale addresses/routes.

    An IPv4 + default route can linger on wlan0 after the association died
    (wpa_supplicant got kicked), which would make a naive check report
    "connected" forever — the daemon would never reopen the setup AP.  Require
    wpa_supplicant to actually report wpa_state=COMPLETED first.
    """
    if not has_ipv4(WLAN_IFACE):
        return False
    if wpa_state() != "COMPLETED":
        return False
    # A static/gateway-less link (e.g. some guest networks) may still be fine
    # if DNS resolves; accept that as connected too.
    return _route_via_wlan() or dns_works()


# --------------------------------------------------------------------------
# Known-networks store (known-wifi.json) -> wpa_supplicant.conf
# --------------------------------------------------------------------------
def load_wlans():
    if not os.path.isfile(STORE_FILE):
        return []
    try:
        with open(STORE_FILE) as f:
            data = json.load(f)
        nets = data.get("networks", []) if isinstance(data, dict) else data
        return [n for n in nets if n.get("ssid")]
    except Exception:
        log.exception("Could not read %s", STORE_FILE)
        return []


def write_wpasupplicant(nets):
    """Write wpa_supplicant.conf (and the per-interface mirror) from the store.

    Bullseye may run either the monolithic service reading WPA_CONF or the
    per-interface wpa_supplicant@wlan0 unit reading wpa_supplicant-wlan0.conf;
    writing both keeps whichever instance owns wlan0 in sync with the store.
    """
    return _write_wpa_conf(WPA_CONF, nets), _write_wpa_conf(WPA_IFACE_CONF, nets)


def safe_write_wpasupplicant(nets):
    """Rewrite wpa_supplicant.conf + the per-interface mirror without crashing.

    A failed write (read-only SD, corrupt store, disk full) must never take the
    daemon down: log it loudly and return False so callers can keep going and
    still reach setup mode instead of crash-looping.
    """
    try:
        write_wpasupplicant(nets)
        return True
    except Exception:
        log.exception("Could not write %s / %s", WPA_CONF, WPA_IFACE_CONF)
        return False


def _force_update_config_off(lines):
    """Force `update_config=0` into the generated head, whatever was preserved.

    known-wifi.json is the ONLY source of truth and this daemon regenerates the
    conf from it on every start, so wpa_supplicant must never write the file
    back.  With `update_config=1` it does exactly that, in two ways that both
    broke this device in the field:

      * its SIGTERM handler saves its own in-memory config over the file we
        just generated (losing networks, or resurrecting stale ones);
      * it persists `disabled=1` for every network a `select_network` call
        disabled — the state that left a Pi unable to reach its home WiFi.

    Normalising here rather than only in the default head is the point: the
    head is *preserved* from whatever file already existed (the OS ships one
    with update_config=1), so a default-only change would be a no-op on the Pi.
    """
    out = [l for l in lines if not l.strip().startswith("update_config=")]
    for i, l in enumerate(out):
        if l.strip().startswith("ctrl_interface="):
            out.insert(i + 1, "update_config=0")
            return out
    return ["update_config=0"] + out


def _resolve_priority(entry):
    """The `priority=` value for one store entry.

    The store carries a boolean `preferred` flag (the UI's star), NOT a
    hand-tuned integer: the star is a star, and the 10/1 split is an internal
    detail of the generator.  Anything truthy counts as preferred, so a
    hand-edited `"preferred": true` works and a missing key is simply not.
    """
    return PREFERRED_PRIORITY if entry.get("preferred") else DEFAULT_PRIORITY


def preferred_ssid(nets):
    """SSID(s) currently flagged preferred — logged so a silent miss is visible."""
    return [n.get("ssid") for n in nets or [] if n.get("ssid") and n.get("preferred")]


def _write_wpa_conf(path, nets):
    lines = []
    # Keep any pre-existing head block (ctrl_interface / country) that the OS
    # already set up, but strip the hardcoded network blocks.
    if os.path.isfile(path) and not wpa_conf_is_managed(path):
        lines = _preserve_non_network_lines(path)
    else:
        lines = [
            "ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev",
            "update_config=0",
            "country=%s" % COUNTRY,
        ]

    # Remove any concatenated, already-existing config from a previous managed run.
    if wpa_conf_is_managed(path):
        lines = _extract_managed_body(path)

    blocks = []
    for n in nets:
        key_mgmt = n.get("key_mgmt", "WPA-PSK")
        psk = _is_psk_hex(n.get("psk"))
        password = n.get("password", "")
        block = [
            "network={",
            '    ssid="%s"' % _escape(n["ssid"]),
            # Always emitted, for every network and on every write: the
            # generated file is the only place wpa_supplicant's ordering is
            # decided, so a `priority=` that silently stops appearing is
            # exactly the bug this line exists to make visible.  The value is
            # conf-only (never stored as an integer), so it is identical on
            # every attach path (@wlan0 unit, direct -B, monolithic).
            "    priority=%d" % _resolve_priority(n),
        ]
        if psk:
            # A 64-hex `psk` (precomputed hash) is the preferred form: the
            # plaintext passphrase never appears on disk.  A psk-only entry
            # ({ssid, psk}, no password key) is fully supported.
            block.append("    psk=" + n["psk"])
        elif password:
            # Plaintext passphrase (hand-edited stores) still works.
            block.append('    psk="%s"' % password.replace('"', '\\"'))
        else:
            block.append("    key_mgmt=NONE")
            if key_mgmt == "WPA-ENTERPRISE":
                pass
        block.append("}")
        blocks.append("\n".join(block))

    # Head = first line(s) of preserved template, then a marker we can find
    # again on the next run to avoid duplication.  Force update_config=0 LAST,
    # so it applies no matter which of the three head sources above was used.
    lines = _force_update_config_off(lines)
    header = "\n".join(lines).strip()
    body = "\n\n".join(blocks) if blocks else ""

    body = ("# %s\n" % _WPA_MANAGED_MARKER) + body + ("\n# /%s" % _WPA_MANAGED_MARKER)

    content = header
    if not content.endswith("\n"):
        content += "\n"
    content += "\n" + body + "\n"

    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(content)
    shutil.move(tmp, path)
    os.chmod(path, 0o600)
    return content


def _escape(s):
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _is_psk_hex(v):
    """True when v is a 64-hex WPA2 precomputed PSK hash (not a passphrase)."""
    if not v or len(v) != 64:
        return False
    try:
        int(v, 16)
        return True
    except (TypeError, ValueError):
        return False


_WPA_MANAGED_MARKER = "WIFI-SETUP-MANAGED"


def wpa_conf_is_managed(path=WPA_CONF):
    if not os.path.isfile(path):
        return False
    try:
        with open(path) as f:
            text = f.read()
        return _WPA_MANAGED_MARKER in text
    except Exception:
        return False


def _extract_managed_body(path):
    """Return just the header lines (before our marker) of a managed file."""
    try:
        with open(path) as f:
            head = f.read()
        head = head.split("# " + _WPA_MANAGED_MARKER)[0]
        return [l for l in head.splitlines() if l.strip()]
    except Exception:
        return []


def _preserve_non_network_lines(path):
    """If the config is a normal hand-written one, keep lines outside network={}."""
    kept, in_net, depth = [], False, 0
    try:
        with open(path) as f:
            for raw in f:
                line = raw.rstrip("\n")
                if line.strip().startswith("network="):
                    in_net, depth = True, 1
                    continue
                if in_net:
                    depth += line.count("{") - line.count("}")
                    if depth <= 0:
                        in_net = False
                    continue
                if line.strip():
                    kept.append(line)
    except Exception:
        return []
    return kept


def _ensure_wpa_ctrl_dir():
    """Make sure WPA_CTRL_DIR exists and is writable by the netdev group.

    On a stock Pi this directory is created at boot by a systemd-tmpfiles rule
    shipped with the wpa_supplicant package.  On minimal/lite images that rule
    can be missing or inactive, and without the directory wpa_supplicant does
    NOT create its per-interface socket — every `wpa_cli -i wlan0` then fails
    with "Failed to connect to non-global ctrl_ifn" while the daemon process
    itself looks perfectly healthy.  Because /run is tmpfs, the directory must
    be ensured on every start, not just at install time.
    """
    try:
        os.makedirs(WPA_CTRL_DIR, mode=0o750, exist_ok=True)
        shutil.chown(WPA_CTRL_DIR, user="root", group="netdev")
        os.chmod(WPA_CTRL_DIR, 0o750)
    except Exception as e:
        log.warning("Could not ensure ctrl dir %s (%s); wpa_cli will be blind",
                    WPA_CTRL_DIR, e)


def _wpa_iface_socket():
    """Path of the per-interface control socket (the thing wpa_cli needs)."""
    return os.path.join(WPA_CTRL_DIR, WLAN_IFACE)


def _wpa_attached():
    """True when wpa_supplicant owns wlan0 AND wpa_cli can actually talk to it.

    A "started" systemd unit does not imply the interface socket exists (the
    monolithic -u -s -O service binds no interface).  This checks the kernel of
    truth: the socket file plus a PONG from it.
    """
    if not os.path.exists(_wpa_iface_socket()):
        return False
    rc, out = run(wpa_cli("ping"), timeout=5)
    return rc == 0 and "PONG" in out


def _wait_attached(seconds=5.0):
    """Poll _wpa_attached() for up to `seconds` (a unit needs a moment to bind)."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        if _wpa_attached():
            return True
        time.sleep(0.5)
    return False


def reconfigure_wifi():
    run(wpa_cli("reconfigure"))


def enable_all_networks(why=""):
    """Re-enable every configured network and restore auto-reconnect.

    `wpa_cli select_network <id>` — which connect_now() uses to honour the
    network the user just picked — DISABLES all the other networks and turns
    auto-reconnect off (documented as "select_network <id>: select a network
    (disable others)").  The device is left stuck on that one network: it can
    never fall back to its other saved networks, e.g. it stays off the home
    WiFi forever after a "Save & connect" to a network that is later out of
    range.

    `enable_network all` is the documented undo: it clears the per-network
    `disabled` flags and resets auto_reconnect / auto_reconnect_disabled, so
    wpa_supplicant goes back to picking the best *available* known network.

    This is defence in depth, not the primary guard: the generated head carries
    `update_config=0`, so wpa_supplicant cannot write the damage to disk even if
    the daemon is SIGKILLed between the `select_network` and this call.  The
    conf is regenerated from the store on every start regardless.
    """
    rc, _ = run(wpa_cli("enable_network", "all"), timeout=10)
    if rc == 0:
        log.info("Re-enabled all saved networks (auto-reconnect restored)%s",
                 " [%s]" % why if why else "")
    else:
        log.warning("'enable_network all' failed (rc=%d)%s — other saved networks "
                    "may stay disabled", rc, " [%s]" % why if why else "")
    return rc == 0


def _stop_all_wpa_supplicant(settle=0.5):
    """Stop every wpa_supplicant that could own wlan0 and let it die.

    Must also run BEFORE we rewrite wpa_supplicant.conf.  The generated head
    now carries `update_config=0`, so a live instance can no longer save over
    the file — but stopping first is kept as defence in depth: a hand-edited or
    OS-shipped conf can still carry update_config=1, and stopping first makes
    the write unconditionally safe.  Writing the conf while a live instance
    exists was a lost update.
    """
    run(["systemctl", "stop", "wpa_supplicant@%s" % WLAN_IFACE])
    run(["systemctl", "stop", "wpa_supplicant"])
    run(["pkill", "-f", "wpa_supplicant"])
    if settle:
        time.sleep(settle)


def _wpa_configured_networks():
    """SSIDs wpa_supplicant currently has configured (None when unreachable)."""
    rc, out = run(wpa_cli("list_networks"), timeout=10)
    if rc != 0:
        return None
    ssids = []
    for line in out.splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) >= 2 and parts[1]:
            ssids.append(parts[1])
    return ssids


def ensure_wpa_supplicant_running():
    """Make sure exactly ONE wpa_supplicant owns wlan0, reading OUR config.

    Deterministic single path:
      1. ensure the control-interface directory exists (wpa_cli cannot reach
         the daemon without it; see _ensure_wpa_ctrl_dir);
      2. stop every competing instance (per-interface unit, monolithic unit,
         or a manually spawned one from dhcpcd / a previous AP session);
      3. attach in this order of preference — each step is VERIFIED by the
         per-interface socket + `wpa_cli ping` before being accepted:
           a. per-interface `wpa_supplicant@wlan0` if enabled (binds wlan0,
              reads the synced mirror wpa_supplicant-wlan0.conf);
           b. direct background `wpa_supplicant -B -i wlan0` with WPA_CONF
              (explicitly binds the interface);
           c. monolithic `wpa_supplicant.service` LAST — it runs with
              `-u -s -O /run/wpa_supplicant` and NO `-i`, so it registers as a
              DBus service but never binds wlan0 on its own; it is only
              attempted when the mirror/conf-generating attach paths above
              could not come up, and its failure is logged loudly.
    Never starts two instances racing for the interface, and never marks the
    interface "attached" unless wpa_cli can actually talk to wlan0.
    """
    _ensure_wpa_ctrl_dir()
    _stop_all_wpa_supplicant()

    candidates = []
    rc, _ = run(["systemctl", "is-enabled", "wpa_supplicant@%s" % WLAN_IFACE])
    if rc == 0:
        candidates.append((["systemctl", "start", "wpa_supplicant@%s" % WLAN_IFACE],
                           "wpa_supplicant@%s" % WLAN_IFACE))
    candidates.append((["wpa_supplicant", "-B", "-i", WLAN_IFACE,
                        "-c", WPA_CONF, "-D", "nl80211"],
                       "direct wpa_supplicant -B"))
    rc, _ = run(["systemctl", "is-enabled", "wpa_supplicant"])
    if rc == 0:
        candidates.append((["systemctl", "start", "wpa_supplicant"],
                           "wpa_supplicant.service"))

    attached = None
    for cmd, label in candidates:
        rc, _ = run(cmd)
        # A started unit needs a moment to create the socket; poll instead of a
        # fixed sleep so a slow-but-fine @wlan0 isn't treated as failed (which
        # would start a second instance racing for the interface).
        if _wait_attached():
            attached = label
            break
        if label == "wpa_supplicant.service":
            log.warning("wpa attach via '%s' produced no interface socket at %s "
                        "(monolithic -u -s runs without -i -> binds nothing); "
                        "direct -B / @-unit are the working paths",
                        label, _wpa_iface_socket())
        else:
            log.info("wpa attach via '%s' not verified yet (socket %s missing "
                     "or no PONG), trying next path",
                     label, _wpa_iface_socket())

    sock = _wpa_iface_socket()
    rc, procs = run(["pgrep", "-af", "wpa_supplicant"])
    if attached:
        log.info("wlan0 attached via %s (%s present, wpa_cli PONG)",
                 attached, sock)
    else:
        log.warning("wpa attach: NO path produced %s; wpa_supplicant procs: %s",
                    sock, procs.strip().replace("\n", " | ") or "(none)")
        log.warning("wpa verify: %s present=%s",
                    WPA_CTRL_DIR, os.path.isdir(WPA_CTRL_DIR))
        return

    # A PONG only proves the control socket answers — NOT that the instance
    # actually READ our networks.  The @-unit reads the wpa_supplicant-wlan0.conf
    # mirror while the direct -B path reads WPA_CONF; if the one that won the
    # attach has a stale/empty file, wpa_supplicant sits there healthy-looking
    # with zero networks and can never associate.  Verify the network list.
    configured = _wpa_configured_networks()
    if configured:
        log.info("wpa_supplicant has %d network(s) configured: %s",
                 len(configured), ", ".join(configured))
    else:
        log.error("wpa_supplicant is attached but has NO networks configured "
                  "(wpa_cli list_networks: %s). It reads %s or %s — check that "
                  "the file the winning attach path uses was written.",
                  configured if configured is not None else "unreachable",
                  WPA_IFACE_CONF, WPA_CONF)


def connect_now(ssid, password):
    """Best-effort switch to a newly saved network USING wpa_cli, no reboot.

    Returns True when the interface shows connected.  The caller should verify
    real connectivity (IP + gateway/DNS) and fall back to a reboot if needed.
    """
    log.info("connect_now: switching wlan0 to '%s' via wpa_cli", ssid)
    mark_connecting(ssid)
    # 1) Stop client wifi FIRST, then regenerate wpa_supplicant.conf.  Order
    #    matters: a conf written while a wpa_supplicant is still alive can be
    #    clobbered on that instance's way out (an OS-shipped conf may still say
    #    update_config=1; ours now forces update_config=0).
    teardown_ap()                       # stops hostapd/dnsmasq, flushes addr
    _stop_all_wpa_supplicant()
    nets = load_wlans()
    if not safe_write_wpasupplicant(nets):
        clear_connect_state()
        return False

    # 2) Attach wlan0 in client mode.
    ensure_wpa_supplicant_running()

    # 3) Ask wpa_supplicant to reload config and select the network.
    run(wpa_cli("reconfigure"))

    # 4) Prefer the network the user just picked.  wpa_cli separates columns
    #    with tabs so SSIDs containing spaces parse correctly.
    rc, out = run(wpa_cli("list_networks"))
    nid = None
    if rc == 0:
        for line in out.splitlines()[1:]:
            parts = line.split("\t")
            if len(parts) >= 2 and parts[1] == ssid:
                nid = parts[0]
                break
    log.info("connect_now: reconfigure sent, network id for '%s' is %s",
             ssid, nid or "(none - reassociate)")
    if nid is not None:
        # select_network forces THIS network now, which is exactly what the
        # user asked for, but it also disables every other network and kills
        # auto-reconnect.  The window is two statements wide and step 6 undoes
        # it; with update_config=0 in the generated head it cannot even reach
        # disk, so an unclean kill here is no longer permanent.
        run(wpa_cli("select_network", nid))
    else:
        run(wpa_cli("reassociate"))

    # 5) Wait for DHCP + link.  Log EVERY sample (not just changes): in the
    #    "associates then vanishes" case wpa_state can sit empty the whole time
    #    (wpa_cli cannot reach the socket) while the RADIO is actually
    #    associating - so also log `iw dev wlan0 link`, which talks straight to
    #    cfg80211 and shows a kernel-level association regardless of wpa_cli.
    log.info("connect_now: waiting up to 30s (each line = one 2s sample)")
    connected = False
    for i in range(15):
        state, cur_ssid, ip = wpa_snapshot()
        rc, iw = run(["iw", "dev", WLAN_IFACE, "link"])
        iwlink = iw.strip().replace("\n", " | ") or "(no output)"
        log.info("connect_now samp%d: wpa_state=%s ssid=%s ip=%s iw=%s",
                 i + 1, state, cur_ssid or "(none)", ip or "(none)", iwlink[:140])
        if ip:
            time.sleep(2)
            if is_connected():
                connected = True
                break
        time.sleep(2)

    # 6) Undo select_network's side effect in BOTH outcomes.  Without this the
    #    device is stuck on this one network for as long as it stays connected:
    #    every other saved network is disabled and auto-reconnect is off, so
    #    coming back to the previous WiFi (or losing this one) cannot self-heal
    #    until the next daemon start.
    enable_all_networks("after live switch to '%s'" % ssid)

    if connected:
        mark_connected(ssid)
        return True
    log.warning("connect_now: wlan0 did not become fully connected in time "
                "(final wpa_state=%s, ssid=%s); handing back to the daemon.",
                wpa_state(), connected_ssid())
    clear_connect_state()   # failed: let the daemon restore the setup AP
    return False


def manual_exit_setup():
    """Leave setup mode and give wlan0 back to wpa_supplicant (no reboot).

    Called when the user asks the web UI to stop the setup AP.  Tears down
    hostapd/dnsmasq, restores the saved wpa_supplicant config, re-attaches
    wlan0 in client mode and lets wpa_supplicant pick the best known network.
    The supervisor (supervise()) then takes over: if nothing connects within
    SETUP_TIMEOUT it simply reopens the setup AP.
    """
    long_log("Leaving setup mode; handing wlan0 back to wpa_supplicant.")
    teardown_ap()
    _stop_all_wpa_supplicant()   # no live instance may re-save over our conf
    nets = load_wlans()
    if nets:
        safe_write_wpasupplicant(nets)
    else:
        log.warning("No saved networks to reconnect to; will drop back into "
                    "setup mode if nothing appears within %ds.", SETUP_TIMEOUT)
    ensure_wpa_supplicant_running()
    reconfigure_wifi()
    # A previous live "Save & connect" may have left the other networks
    # disabled; make sure every saved network is a candidate again.
    enable_all_networks("after leaving setup mode")
    run(["pkill", "-f", "wifi_config_server"])


# --------------------------------------------------------------------------
# Setup AP bring-up / tear-down
# --------------------------------------------------------------------------
def _dhcpcd_active():
    return run(["systemctl", "is-active", "dhcpcd"])[0] == 0


def pause_dhcpcd():
    """Stop dhcpcd so it can't fight the setup AP's static 192.168.4.1 address.

    Raspberry Pi OS Bullseye lets dhcpcd own wlan0 (wpa hook + DHCP).  While
    hostapd is up that would re-DHCP wlan0 and steal/refresh the static AP
    address, killing the AP.  The stop is recorded in a marker file so a crash
    mid-AP can restore dhcpcd on the next daemon start.  Best-effort: a dhcpcd
    that was never running is left alone.
    """
    if not _dhcpcd_active():
        return
    rc, _ = run(["systemctl", "stop", "dhcpcd"])
    if rc == 0:
        try:
            os.makedirs(os.path.dirname(DHCPCD_PAUSE_MARKER) or ".", exist_ok=True)
            with open(DHCPCD_PAUSE_MARKER, "w") as f:
                f.write(str(int(time.time())))
        except OSError:
            pass
        log.info("dhcpcd stopped (setup AP owns %s)", WLAN_IFACE)


def resume_dhcpcd():
    """Restart dhcpcd if we paused it, removing the marker.

    Returns True when dhcpcd was actually restored, False when it was never
    paused (or the unit is absent).  Safe to call at any time — it is the
    crash-recovery path too.
    """
    if not os.path.isfile(DHCPCD_PAUSE_MARKER):
        return False
    try:
        os.remove(DHCPCD_PAUSE_MARKER)
    except OSError:
        pass
    rc, _ = run(["systemctl", "start", "dhcpcd"])
    log.info("dhcpcd restarted (may manage %s again)", WLAN_IFACE)
    return rc == 0


def write_hostapd_conf():
    """Write /etc/hostapd/hostapd.conf.  Returns False on error (never raises)."""
    rc, _ = run(["iw", "dev", WLAN_IFACE, "info"])
    if rc != 0:
        log.error("'iw dev %s info' failed; is wireless-regdb / iw installed?", WLAN_IFACE)
    driver = "nl80211"
    content = "\n".join([
        "interface=" + WLAN_IFACE,
        "driver=" + driver,
        "ssid=" + AP_SSID,
        "hw_mode=g",
        "channel=" + AP_CHANNEL,
        "wmm_enabled=1",
        "macaddr_acl=0",
        "auth_algs=1",
        "ignore_broadcast_ssid=0",
    ])
    if AP_PASSWORD:
        content += "\n" + "\n".join([
            "wpa=2",
            "wpa_passphrase=" + AP_PASSWORD,
            "wpa_key_mgmt=WPA-PSK",
            "rsn_pairwise=CCMP",
        ])
    try:
        os.makedirs(os.path.dirname(HOSTAPD_CONF) or ".", exist_ok=True)
        tmp = HOSTAPD_CONF + ".tmp"
        with open(tmp, "w") as f:
            f.write(content + "\n")
        shutil.move(tmp, HOSTAPD_CONF)
        os.chmod(HOSTAPD_CONF, 0o600)
    except Exception:
        log.exception("Could not write %s", HOSTAPD_CONF)
        return False
    return True


def dnsmasq_conf_text():
    """Content of the setup-AP dnsmasq conf (pure; no I/O)."""
    # dnsmasq 2.86+ (Bullseye) has --bind-dynamic as the default; setting
    # --bind-interfaces alongside it is a hard startup error.  bind-dynamic
    # serves only the interfaces that exist (incl. wlan0), so DHCP never leaks
    # to eth0/usb0.  dhcp-authoritative makes joins of devices with stale
    # leases from other networks answer instantly (reconnect speed).
    lines = [
        "interface=" + WLAN_IFACE,
        "bind-dynamic",
        "dhcp-authoritative",
        "dhcp-range=192.168.4.2,192.168.4.100,255.255.255.0,12h",
        "dhcp-option=option:router," + AP_IP,
        "dhcp-option=option:dns-server," + AP_IP,
        "address=/%s/%s" % (SETUP_DOMAIN, AP_IP),
    ]
    if CAPTIVE_PORTAL:
        # Wildcard: every A query -> AP_IP (AAAA gets an empty answer).  Safe
        # for the daemon's own is_connected(): dnsmasq only runs while hostapd
        # owns wlan0, and there wpa_state can never be COMPLETED.  No DHCP
        # option 114 (RFC 8910): iOS would expect an RFC 8908 JSON API behind
        # it; the classic probe redirect is enough.
        lines.append("address=/#/" + AP_IP)
    return "\n".join(lines)


def write_dnsmasq_conf():
    """Write /etc/dnsmasq.d/wifi-setup.conf.  Returns False on error."""
    content = dnsmasq_conf_text()
    try:
        os.makedirs("/etc/dnsmasq.d", exist_ok=True)
        tmp = DNSMASQ_CONF + ".tmp"
        with open(tmp, "w") as f:
            f.write(content + "\n")
        shutil.move(tmp, DNSMASQ_CONF)
        os.chmod(DNSMASQ_CONF, 0o644)
    except Exception:
        log.exception("Could not write %s", DNSMASQ_CONF)
        return False
    return True


def _ap_interface_up():
    """True when the kernel really has wlan0 in AP mode (not just "hostapd ran")."""
    rc, out = run(["iw", "dev", WLAN_IFACE, "info"])
    return rc == 0 and "type AP" in out


def _ensure_service_active(name, what):
    """Restart is not proof: verify a service is active, retry once, warn loud.

    The setup AP silently "works" without DHCP/DNS if dnsmasq died — never
    assume it.  Returns True when the service is active after the checks.
    """
    def _is_active():
        return run(["systemctl", "is-active", name])[0] == 0

    run(["systemctl", "restart", name])
    if _is_active():
        return True
    log.warning("%s did not become active after restart; retrying once.", name)
    run(["systemctl", "stop", name])
    run(["systemctl", "start", name])
    time.sleep(2)
    if _is_active():
        return True
    rc, out = run(["systemctl", "status", name, "--no-pager", "-l"])
    log.error("%s is NOT active — the setup %s will not work. "
              "Check: systemctl status %s (%s)",
              name, what, name, out.strip()[-300:])
    # `systemctl status` rarely shows WHY a unit failed; the journal does.
    rc, jout = run(["journalctl", "-u", name, "-n", "15", "--no-pager"])
    if rc == 0 and jout.strip():
        for line in jout.strip().splitlines():
            log.error("%s journal: %s", name, line)
    return False


def setup_ap():
    """Bring up the setup AP.  Returns True only when it is really usable.

    Verifying matters: a failed hostapd used to be reported as success, and the
    caller's retry loop then re-ran this every AP_RESTORE_GRACE seconds
    forever — the device sat in a silent "no client WiFi, no setup AP" limbo
    with nothing but a log line to show for it.
    """
    log.info("Starting setup AP (%s)", AP_SSID)

    # dhcpcd would fight the static AP address and re-DHCP wlan0; pause it and
    # remember to restore it when the AP comes down.
    pause_dhcpcd()

    # Stop client wifi on wlan0 first (both the monolithic and the @-unit form).
    _stop_all_wpa_supplicant(settle=1.0)

    if not write_hostapd_conf():
        log.error("Could not write %s; the setup AP will not work.", HOSTAPD_CONF)
    if not write_dnsmasq_conf():
        log.error("Could not write %s; DHCP/DNS on the setup AP will not work.",
                  DNSMASQ_CONF)

    run(["ip", "link", "set", WLAN_IFACE, "down"])
    run(["ip", "addr", "flush", "dev", WLAN_IFACE])
    run(["ip", "addr", "add", AP_IP + "/24", "dev", WLAN_IFACE])
    run(["ip", "link", "set", WLAN_IFACE, "up"])

    dnsmasq_ok = _ensure_service_active("dnsmasq", "AP DHCP/DNS")
    hostapd_ok = _ensure_service_active("hostapd", "WiFi access point")
    in_ap_mode = _ap_interface_up()
    ap_up = bool(dnsmasq_ok and hostapd_ok and in_ap_mode)
    if not ap_up:
        # Never just assume the AP works: surface the failure loudly but keep
        # the daemon alive so the user can still reach SSH at 192.168.4.1.
        rc, info = run(["iw", "dev", WLAN_IFACE, "info"])
        long_log("Setup AP did NOT come up (dnsmasq=%s hostapd=%s ap_mode=%s). "
                 "wlan0 owner per iw: %s",
                 dnsmasq_ok, hostapd_ok, in_ap_mode,
                 info.strip().replace("\n", " | ")[:160] or "(iw failed)")
    return ap_up


def teardown_ap():
    log.info("Tearing down setup AP")
    run(["systemctl", "stop", "hostapd"])
    run(["systemctl", "stop", "dnsmasq"])
    # Deliberately NOT `systemctl disable` here: we never enabled hostapd/
    # dnsmasq ourselves (start/stop is explicit, see setup_ap), so disabling
    # would only clobber a pre-existing "enabled" state the user chose.
    # Boot ordering in the systemd unit is what keeps an (unlikely) enabled
    # hostapd from grabbing wlan0 before we run.
    run(["ip", "addr", "flush", "dev", WLAN_IFACE])
    run(["ip", "link", "set", WLAN_IFACE, "down"])
    run(["ip", "link", "set", WLAN_IFACE, "up"])
    # Give wlan0 back to dhcpcd so DHCP + the wpa hook work in client mode.
    resume_dhcpcd()


# --------------------------------------------------------------------------
# HTTP/HTTPS config server (stdlib http.server) as a child process
# --------------------------------------------------------------------------
def ensure_config_cert():
    """Generate a self-signed cert for the config server if missing.

    HTTPS protects the WiFi password the user types in the web form from being
    sniffed on the (possibly open or fellow-device) setup AP.  A self-signed
    cert is enough here because the threat is on-link eavesdropping, not CA
    trust.  Only runs when root, so it's a no-op in a test/local run.
    """
    if os.path.isfile(CERT_FILE) and os.path.isfile(KEY_FILE):
        if os.environ.get("WIFI_CERT") or _cert_matches_domain():
            return   # a user-supplied cert is never touched
        log.info("Setup domain is now %s (hostname changed?); regenerating the "
                 "self-signed cert", SETUP_DOMAIN)
    os.makedirs(os.path.dirname(CERT_FILE), exist_ok=True)
    base = ["openssl", "req", "-x509", "-newkey", "rsa:2048",
            "-keyout", KEY_FILE, "-out", CERT_FILE, "-days", "825",
            "-nodes", "-subj", "/CN=" + SETUP_DOMAIN]
    # SAN for both ways the page is opened; -addext needs OpenSSL 1.1.1+
    # (Bullseye ships it), so retry without it on anything older.
    rc, out = run(base + ["-addext", "subjectAltName=DNS:%s,IP:%s"
                          % (SETUP_DOMAIN, AP_IP)])
    if rc != 0:
        rc, out = run(base)
    if rc != 0:
        log.warning("Could not generate self-signed cert (%s); falling back to HTTP", out.strip())
        return
    try:
        os.chmod(KEY_FILE, 0o600)
        os.chmod(CERT_FILE, 0o644)
    except OSError:
        pass
    log.info("Generated self-signed cert %s", CERT_FILE)


def _cert_matches_domain():
    """True when the existing self-signed cert was issued for SETUP_DOMAIN."""
    rc, out = run(["openssl", "x509", "-in", CERT_FILE, "-noout", "-subject"])
    return rc != 0 or SETUP_DOMAIN in out   # unreadable: leave it alone


_cfg_server_proc = None   # the spawned config-server child (watchdog target)


def start_config_server():
    global _cfg_server_proc
    run(["pkill", "-f", "wifi_config_server"])
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "wifi_config_server.py")
    env = dict(os.environ)
    # Bind only to the setup AP address, never 0.0.0.0: the web page must only
    # be reachable from devices on the AP, not from other interfaces.
    env["WIFI_CONFIG_HOST"] = AP_IP
    # Hand over the names exactly as resolved here, so page, AP and DNS agree.
    env["WIFI_DEVICE_NAME"] = DEVICE_NAME
    env["WIFI_AP_SSID"] = AP_SSID
    env["WIFI_SETUP_DOMAIN"] = SETUP_DOMAIN
    if SETUP_EXPIRES_AT:
        env["WIFI_SETUP_EXPIRES_AT"] = str(int(SETUP_EXPIRES_AT))
    if os.path.isfile(CERT_FILE) and os.path.isfile(KEY_FILE):
        env["WIFI_CERT"] = CERT_FILE
        env["WIFI_KEY"] = KEY_FILE
        port = int(os.environ.get("WIFI_CONFIG_PORT", CONFIG_PORT))
        if "WIFI_CONFIG_PORT" not in os.environ:
            port = 443
        env["WIFI_CONFIG_PORT"] = str(port)
        # Plain-HTTP listener that just 301s to https, so typing
        # http://192.168.4.1 doesn't show a raw connection error.
        env["WIFI_REDIRECT_HTTP_PORT"] = os.environ.get("WIFI_REDIRECT_HTTP_PORT", "80")
    else:
        # Default to HTTP on 80 unless explicitly overridden.
        env.setdefault("WIFI_CONFIG_PORT", "80")
    _cfg_server_proc = subprocess.Popen(
        [sys.executable, script, env.get("WIFI_CONFIG_PORT", "80")],
        env=env)
    return _cfg_server_proc


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------
class BufferedRotatingHandler(logging.Handler):
    """Buffer log records in memory and write them out in batches.

    Event-triggered flushing (NOT a fixed poll timer):

      - WARNING/ERROR/CRITICAL flush to disk immediately — those are exactly
        the lines a human is watching on /api/log while debugging (e.g.
        "connect failed", "AP conflict").
      - INFO and below (re)arm a short one-shot debounce timer; when it fires
        the pending batch is written.  That is "at most FLUSH_DEBOUNCE late",
        not "poll every FLUSH_DEBOUNCE".
      - A timer tick with an empty buffer returns without any I/O: no flush(),
        no open(), no file touch.  (In practice the timer only exists while
        records are buffered — flush()/emit cancel it as soon as they write.)

    Disk writes + rotation (1MB, one `.1` backup) go through a standard
    logging.handlers.RotatingFileHandler.  Batch records are written with
    handler.handle(), which takes the handler's own lock, so concurrent
    batches (timer thread + immediate WARNING flush) are serialized.

    Safety net against data loss: the debounced/immediate flush is the real
    protection.  atexit and the SIGTERM handler in main() are best-effort
    only — a Pi Zero in the field can lose power / get SIGKILL'd at any
    moment, so we never *rely* on a clean shutdown to persist the log.
    """

    MAX_BYTES = int(os.environ.get("WIFI_LOG_MAX_BYTES", str(1_048_576)))  # 1 MB
    BACKUP_COUNT = int(os.environ.get("WIFI_LOG_BACKUP", "1"))             # + daemon.log.1
    FLUSH_DEBOUNCE = 1.0            # upper bound for buffered (INFO) records
    IMMEDIATE_LEVEL = logging.WARNING
    MAX_BUFFERED_RECORDS = 500      # hard cap; spill the whole batch at once

    def __init__(self, path):
        super().__init__()
        self._buf = []
        self._lock = threading.Lock()
        self._timer = None
        self._rfh = logging.handlers.RotatingFileHandler(
            path, maxBytes=self.MAX_BYTES, backupCount=self.BACKUP_COUNT,
            delay=True, encoding="utf-8")

    def setFormatter(self, fmt):
        logging.Handler.setFormatter(self, fmt)
        # The same format is used for the disk writes (which produce the
        # rotation-triggering file contents).
        self._rfh.setFormatter(fmt)

    def emit(self, record):
        try:
            with self._lock:
                self._buf.append(record)
                immediate = (record.levelno >= self.IMMEDIATE_LEVEL
                             or len(self._buf) >= self.MAX_BUFFERED_RECORDS)
            if immediate:
                self.flush()        # WARNING+ (or overflow) -> disk right now
            else:
                self._arm_timer()   # INFO: reset debounce for this last record
        except Exception:
            self.handleError(record)

    def _arm_timer(self):
        with self._lock:
            self._cancel_timer_locked()
            t = threading.Timer(self.FLUSH_DEBOUNCE, self._on_timer)
            t.daemon = True
            t.start()
            self._timer = t

    def _on_timer(self):
        with self._lock:
            self._timer = None
            if not self._buf:
                return              # nothing buffered: no I/O at all
            batch, self._buf = self._buf, []
        self._write_batch(batch)

    def flush(self):
        with self._lock:
            self._cancel_timer_locked()
            if not self._buf:
                return              # nothing buffered: no I/O at all
            batch, self._buf = self._buf, []
        self._write_batch(batch)

    def _cancel_timer_locked(self):
        if self._timer is not None:
            try:
                self._timer.cancel()
            except Exception:
                pass
            self._timer = None

    def _write_batch(self, batch):
        try:
            for rec in batch:
                self._rfh.handle(rec)   # handle() serializes emit() under lock
        except Exception:
            pass


def _flush_logs():
    for h in logging.getLogger().handlers:
        try:
            h.flush()
        except Exception:
            pass


def _handle_sigterm(signum, frame):
    """Best-effort shutdown on systemd's SIGTERM (service stop/restart).

    Flushing is best-effort: the debounced+immediate flush is the real safety
    net against power loss / SIGKILL.  Only tear the AP down if it is actually
    up, so a plain restart doesn't bounce wlan0 for no reason.
    """
    log.info("SIGTERM received; flushing log, leaving setup mode")
    rc, _ = run(["systemctl", "is-active", "hostapd"])
    if rc == 0:
        teardown_ap()
    run(["pkill", "-f", "wifi_config_server"])   # don't orphan the web UI
    _flush_logs()
    # Exit 0 so systemd treats a planned stop as success (Restart=on-failure
    # must NOT respawn us after `systemctl stop`).  Also unwinds atexit flush.
    sys.exit(0)


class _UptimeFilter(logging.Filter):
    """Stamp every record with the time since boot (`[up H:MM:SS]`).

    The Pi Zero W has no RTC: after a power cut the wall clock resumes from the
    last saved time and is wrong until NTP syncs, so `asctime` alone can be an
    hour or more off.  CLOCK_MONOTONIC (time.monotonic() on Linux) counts from
    boot and is never stepped, so this field stays trustworthy either way.
    """

    def filter(self, record):
        up = int(time.monotonic())
        record.uptime = "%d:%02d:%02d" % (up // 3600, up // 60 % 60, up % 60)
        return True


def configure_logging():
    fmt = logging.Formatter("%(asctime)s [up %(uptime)s] %(levelname)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    sh.addFilter(_UptimeFilter())
    root.addHandler(sh)
    try:
        os.makedirs(os.path.dirname(LOG_FILE) or ".", exist_ok=True)
        fh = BufferedRotatingHandler(LOG_FILE)
        fh.setFormatter(fmt)
        fh.addFilter(_UptimeFilter())
        root.addHandler(fh)
    except Exception:
        pass
    atexit.register(_flush_logs)


# Wall-clock minus monotonic clock.  Constant while the wall clock runs freely;
# it jumps when NTP (or anyone) steps the clock.  Reset by _log_boot_reference().
_clock_offset = time.time() - time.monotonic()
CLOCK_STEP_THRESHOLD = 5   # seconds; slews/jitter stay well below this


def _ntp_synchronized():
    """True/False when systemd knows the sync state, None when unknown."""
    if os.path.exists("/run/systemd/timesync/synchronized"):
        return True
    rc, out = run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
                  timeout=5)
    out = out.strip().lower()
    if rc == 0 and out in ("yes", "no"):
        return out == "yes"
    return None


def _log_boot_reference():
    """Log where this boot sits in time, so a skewed wall clock is obvious.

    Deliberately never WAITS for NTP: the daemon is what brings the network
    (or the setup AP) up in the first place, so blocking on a clock sync with
    no network would leave the device with neither — unreachable.
    """
    global _clock_offset
    _clock_offset = time.time() - time.monotonic()
    try:
        with open("/proc/sys/kernel/random/boot_id") as f:
            boot_id = f.read().strip()
    except OSError:
        boot_id = "(unknown)"
    synced = _ntp_synchronized()
    up = int(time.monotonic())
    log.info("Boot reference: booted at %s by the wall clock (uptime %ds), "
             "boot_id=%s, NTP synchronized=%s",
             time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(_clock_offset)),
             up, boot_id,
             {True: "yes", False: "no"}.get(synced, "unknown"))
    if synced is False:
        log.warning("Wall clock is not NTP-synced yet (no RTC) — timestamps "
                    "may be off until it syncs; the [up H:MM:SS] field counts "
                    "from boot and is always right. A 'Wall clock stepped' line "
                    "will mark the moment it is corrected.")


def _check_clock_step():
    """Log once per step when the wall clock jumps (typically: NTP synced).

    Pure arithmetic, no I/O — safe to call on every loop iteration.
    """
    global _clock_offset
    offset = time.time() - time.monotonic()
    step = offset - _clock_offset
    if abs(step) < CLOCK_STEP_THRESHOLD:
        return
    _clock_offset = offset
    long_log("Wall clock stepped by %+ds (NTP sync?) — log timestamps before "
             "this line were off by that much; the [up H:MM:SS] field was not "
             "affected." % round(step))


def _ensure_single_instance():
    """Kill any previously-running daemon (via its pidfile), NOT ourselves.

    pkill -f <script> would match this very process's own command line and
    instantly kill the freshly-started daemon, so we track the pid instead.
    """
    try:
        if os.path.isfile(PID_FILE):
            with open(PID_FILE) as f:
                old = f.read().strip()
            if old and _pid_is_daemon(int(old)):
                os.kill(int(old), 15)  # SIGTERM prior instance
    except (OSError, ValueError):
        pass
    try:
        os.makedirs(os.path.dirname(PID_FILE) or ".", exist_ok=True)
        with open(PID_FILE, "w") as f:
            f.write(str(os.getpid()))
    except OSError:
        pass


def _pid_is_daemon(pid):
    """True if the PID belongs to a prior wifi_setup_daemon instance.

    A stale pidfile (crash/power-loss) can point at an unrelated process that
    reused the PID; never signal that.
    """
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as f:
            cmd = f.read().decode("utf-8", "replace").replace("\0", " ")
    except OSError:
        return False
    return "wifi_setup_daemon.py" in cmd


def scan_and_cache():
    """Scan wlan0 (client mode) and cache the results for the config server.

    Best-effort and never fatal: a failed scan must not block the setup AP.
    The Pi Zero W's brcmfmac cannot scan while hostapd owns the radio, so we
    scan while wlan0 is still in client/managed mode and the web UI serves
    the cache.
    """
    nets, detail = wifi_scan.scan_networks(WLAN_IFACE)
    wifi_scan.write_cache(nets, SCAN_CACHE_FILE)
    if nets:
        log.info("Pre-scan captured %d network(s) for the setup page", len(nets))
    else:
        log.warning("Pre-scan found no networks (%s)", detail)
    return nets


def _scan_before_ap():
    """Scan while wlan0 is still free of associations, then cache the results.

    Stop wpa_supplicant first: a mid-association brcmfmac fails iw scan with
    "Device or resource busy" (which is exactly the 0-network pre-scan we saw).
    setup_ap() stops it again of course — idempotent.
    """
    _stop_all_wpa_supplicant(settle=1.0)
    scan_and_cache()


def _enter_setup_mode(with_scan=True):
    """Bring up the setup AP + config server (idempotent, live, no reboot).

    Returns True when the AP is actually up.  A False return means hostapd (or
    the AP-mode switch) failed; the caller decides whether to retry or to fall
    back to plain client mode.
    """
    global SETUP_EXPIRES_AT
    # Any connect-state from an earlier live switch is stale now.
    clear_connect_state()
    long_log("Entering setup mode: starting AP '%s' + config server" % AP_SSID)
    # Capture a fresh network list BEFORE hostapd takes wlan0 over; the config
    # server serves this cache so the page shows nearby networks instantly.
    if with_scan:
        _scan_before_ap()
    ap_up = setup_ap()
    ensure_config_cert()
    start_config_server()
    cert_present = os.path.isfile(CERT_FILE) and os.path.isfile(KEY_FILE)
    scheme = "https" if cert_present else "http"
    port = int(os.environ.get("WIFI_CONFIG_PORT", "443" if cert_present else "80"))
    if ap_up:
        log.info("Config server running on %s://%s:%d", scheme, AP_IP, port)
    else:
        long_log("Config server bound to %s:%d, but the setup AP is DOWN — the "
                 "page is NOT reachable over WiFi until hostapd works."
                 % (AP_IP, port))
    if AP_PASSWORD:
        SETUP_EXPIRES_AT = None
    else:
        # Open AP must not linger: schedule an automatic power-off.
        SETUP_EXPIRES_AT = time.time() + AP_AUTO_OFF_SECONDS
        long_log("Setup AP has NO password; powering the device off in %ds"
                 % AP_AUTO_OFF_SECONDS)
    return ap_up


def _rescan():
    """Pause the setup AP, scan in client mode, then bring the AP back up.

    The phone viewing the page drops off the AP for a few seconds; the web UI
    retries /api/scan and shows the fresh list once the AP returns.
    """
    long_log("Rescan requested; pausing the setup AP to scan in client mode.")
    run(["systemctl", "stop", "hostapd"])
    run(["systemctl", "stop", "dnsmasq"])
    run(["ip", "addr", "flush", "dev", WLAN_IFACE])
    run(["ip", "link", "set", WLAN_IFACE, "down"])
    run(["ip", "link", "set", WLAN_IFACE, "up"])
    time.sleep(1)
    scan_and_cache()
    _enter_setup_mode(with_scan=False)


def _maybe_poweroff():
    """Power the device off when an open setup AP's grace period expires."""
    global SETUP_EXPIRES_AT
    if SETUP_EXPIRES_AT and time.time() >= SETUP_EXPIRES_AT:
        # Only power off while the open AP is genuinely still up.  A live
        # "Save & connect" tears hostapd down and may still be mid-connect; if
        # the AP is gone the deadline is void and the supervisor takes over.
        rc, _ = run(["systemctl", "is-active", "hostapd"])
        if rc != 0:
            SETUP_EXPIRES_AT = None
            return False
        long_log("Open setup AP expired (no password). Powering off.")
        run(["systemctl", "poweroff"])
        SETUP_EXPIRES_AT = None
        return True
    return False


def _setup_mode_loop():
    """Keep the AP + server up until setup mode is over.

    Ends (returns False -> resume supervision) when either:
      - the config server reported a successful live connect, or
      - the user asked to leave setup mode (manual exit; /api/normal), or
      - wlan0 is back in client mode and online (is_connected).
    Returns True when the open-AP deadline powered the device off.

    Self-heals three failure states while it loops:
      - if the config server child died, respawn it;
      - if hostapd was torn down by a live "Save & connect" that neither
        succeeded nor is still in flight, restore the setup AP (AP_RESTORE_GRACE)
        so the page the user is on comes back;
      - if the AP could not be brought up AP_FAIL_LIMIT times in a row, give up
        on it and fall back to plain client-mode supervision.  Retrying forever
        would leave the device with neither a client link nor a setup AP, i.e.
        unreachable over WiFi in any direction.
    """
    global SETUP_EXPIRES_AT
    ap_down_since = None
    ap_failures = 0
    while True:
        time.sleep(2)
        _check_clock_step()
        if _maybe_poweroff():
            return True

        # Watchdog: keep the web UI alive.
        if _cfg_server_proc is not None and _cfg_server_proc.poll() is not None:
            long_log("Config server exited (%d); restarting it."
                     % _cfg_server_proc.returncode)
            start_config_server()

        # A live switch that succeeded (either verdict) ends setup mode.
        state = read_connect_state()
        if state and state.get("connected"):
            long_log("WiFi configured via web UI (SSID=%s); resuming supervision."
                     % (state.get("ssid") or "?"))
            SETUP_EXPIRES_AT = None
            clear_connect_state()
            return False

        if state and state.get("exit_setup"):
            long_log("Manual exit from setup mode requested; reconnecting to a saved network.")
            SETUP_EXPIRES_AT = None
            clear_connect_state()
            manual_exit_setup()
            return False

        if state and state.get("rescan"):
            clear_connect_state()
            _rescan()
            continue

        rc, _ = run(["systemctl", "is-active", "hostapd"])
        if rc == 0:
            ap_down_since = None
            ap_failures = 0
            continue

        # hostapd is down -> wlan0 should be in client mode.  Did we get a
        # network back (the live switch, or wpa_supplicant self-healing)?
        if is_connected():
            long_log("wlan0 back online in client mode (SSID=%s); resuming supervision."
                     % (connected_ssid() or "?"))
            SETUP_EXPIRES_AT = None
            clear_connect_state()
            return False

        if state and state.get("connecting") \
                and time.time() - (state.get("at") or 0) < CONNECT_STATE_STALE:
            ap_down_since = None     # an attempt is in flight; wait it out
            continue
        if state and state.get("connecting"):
            clear_connect_state()    # stale hint: let restore take over

        # Teardown without a successful connect (live attempt failed, or the
        # connecting hint went stale): restore the setup AP so the UI returns.
        if ap_down_since is None:
            log.info("Setup AP is down; will restore in %ds if no network appears."
                     % AP_RESTORE_GRACE)
            ap_down_since = time.time()
        elif time.time() - ap_down_since >= AP_RESTORE_GRACE:
            long_log("No network after teardown; restoring the setup AP.")
            if _enter_setup_mode():
                ap_failures = 0
            else:
                ap_failures += 1
                if ap_failures >= AP_FAIL_LIMIT:
                    # The AP is not coming back.  Rather than spin silently,
                    # hand wlan0 back to wpa_supplicant so the device at least
                    # keeps trying every saved network on its own.
                    long_log("Setup AP failed %d times in a row; giving up on it "
                             "for now and returning to client mode. Saved networks "
                             "stay in %s and keep being tried by wpa_supplicant. "
                             "No restart needed: supervision continues, and the "
                             "setup AP is retried automatically the next time the "
                             "link is down for %ds."
                             % (ap_failures, STORE_FILE, SETUP_TIMEOUT))
                    SETUP_EXPIRES_AT = None
                    clear_connect_state()
                    manual_exit_setup()
                    return False
            ap_down_since = None


def supervise():
    """Keep-alive watchdog.

    wpa_supplicant normally handles reconnecting to saved networks by itself, so
    we do NOT try to reconnect here.  We only watch the clock: if the link stays
    down for longer than SETUP_TIMEOUT, drop into setup mode so the user can
    reconfigure live (no reboot).  When the link is up we just sleep and reset
    the downtime counter.

    One deliberate exception: a single self-heal attempt a few seconds into an
    outage.  "wpa_supplicant reconnects on its own" is only true while its
    network list is intact — a past `select_network` (Save & connect) or a
    config file clobbered by an exiting instance can leave every saved network
    disabled, and then waiting produces nothing at all.  Re-enabling all
    networks and reassociating once costs one scan and makes the device fall
    back to ANY saved network that is in range, before we bother the user with
    the setup AP.
    """
    down_since = None
    last_sig = None
    heal_at = 0.0
    while True:
        time.sleep(POLL_INTERVAL)
        _check_clock_step()
        if is_connected():
            if down_since is not None:
                long_log("Network recovered after %.0fs (SSID=%s)."
                         % (time.time() - down_since, connected_ssid()))
                down_since = None
            continue
        if down_since is None:
            down_since = time.time()
            heal_at = down_since + SETUP_TIMEOUT / 3.0
            state, ssid, ip = wpa_snapshot()
            last_sig = (state, ssid, ip)
            long_log("WiFi link lost (%s): wpa_state=%s ssid=%s ip=%s. "
                     "wpa_supplicant will keep retrying."
                     % (WLAN_IFACE, state, ssid or "(none)", ip or "(none)"))
        else:
            sig = wpa_snapshot()
            if sig != last_sig:
                last_sig = sig
                long_log("still down: wpa_state=%s ssid=%s ip=%s"
                         % sig)
            if heal_at and time.time() >= heal_at:
                # Exactly once per outage.
                heal_at = 0.0
                configured = _wpa_configured_networks()
                log.info("Self-heal attempt: re-enabling all saved networks "
                         "(configured: %s) and reassociating.",
                         ", ".join(configured) if configured else "(none/unreachable)")
                if run(wpa_cli("ping"), timeout=10)[0] != 0:
                    # Both the undo and `reassociate` need the control socket, so
                    # without it the heal above is a guaranteed no-op and the
                    # device just idles until the setup AP opens.  A dead
                    # wpa_supplicant is its own failure mode: bring it back on
                    # OUR conf (it is never rewritten here, so the generated
                    # networks are intact) and only then re-associate.
                    log.warning("wpa_supplicant unreachable (self-heal) — "
                                "restarting it, then re-associating")
                    ensure_wpa_supplicant_running()
                enable_all_networks("supervisor self-heal")
                run(wpa_cli("reassociate"), timeout=15)
        if (time.time() - down_since) >= SETUP_TIMEOUT:
            long_log("No network for %ds. Opening setup mode." % SETUP_TIMEOUT)
            _enter_setup_mode()
            if _setup_mode_loop():
                return   # open-AP expiry powered the device off
            # A live web-UI reconnect succeeded: resume the watchdog.
            down_since = None
            heal_at = 0.0


def long_log(msg):
    """Log a notable state change to the journal AND the tail-able app log."""
    log.warning(msg)


def main():
    if "--diagnose" in sys.argv:
        diagnose()
        return 0
    configure_logging()
    log.info("wifi-setup daemon starting on '%s' (supervisor mode; "
             "setup AP '%s', page %s)", DEVICE_NAME, AP_SSID, SETUP_DOMAIN)
    _log_boot_reference()
    pw_problem = AP_PASSWORD and ap_password_problem(AP_PASSWORD)
    if pw_problem:
        log.warning("WIFI_AP_PASSWORD %s. Change it in %s and restart the "
                    "service.", pw_problem, os.path.join(CFG_DIR, "wifi-setup.env"))
    if COUNTRY_GUESSED:
        log.warning("No WiFi country configured (WIFI_COUNTRY, wpa_supplicant "
                    "conf or /etc/default/crda) - using %s. Set WIFI_COUNTRY in "
                    "%s to your country's code.", COUNTRY,
                    os.path.join(CFG_DIR, "wifi-setup.env"))
    signal.signal(signal.SIGTERM, _handle_sigterm)

    # 1) Make sure we are the single daemon instance.
    _ensure_single_instance()

    # 1a) Repair a dhcpcd left paused by a crashed setup-AP session, so normal
    # client management (DHCP + the wpa hook) works again before we touch wlan0.
    resume_dhcpcd()

    # 1b) Stop any boot-enabled or leftover hostapd/dnsmasq before touching
    # wlan0 — the daemon starts them itself when needed.  We do NOT disable
    # them (that would clobber a state the user chose; the systemd unit's
    # After=hostapd.service dnsmasq.service ordering makes this deterministic).
    run(["systemctl", "stop", "hostapd"])
    run(["systemctl", "stop", "dnsmasq"])
    # Flush any stale addresses/routes a crashed or remote session left on
    # wlan0 — else is_connected() could see a ghost IP/route and believe the
    # link is up when it is not (the "neither client link nor setup mode"
    # limbo).
    run(["ip", "addr", "flush", "dev", WLAN_IFACE])
    run(["ip", "link", "set", WLAN_IFACE, "up"])
    clear_connect_state()   # drop stale connect-state from a past session

    # 2) Regenerate wpa_supplicant from the store and try to attach.
    #    Stop wpa_supplicant BEFORE writing the conf: systemd may already have
    #    started wpa_supplicant@wlan0 (the unit is enabled), and a live
    #    instance reading a conf that still says update_config=1 would save its
    #    own — possibly stale, possibly carrying disabled=1 flags — state back
    #    over the file we generate here.  The generated head forces
    #    update_config=0 so this cannot happen at all in normal operation.
    nets = load_wlans()
    log.info("Loaded %d known network(s) from %s", len(nets), STORE_FILE)
    # Say out loud which network is the favourite.  A `preferred` flag that
    # never reached the store (or a hand edit that got clobbered) is otherwise
    # invisible: the conf just quietly has no priority=10 and wpa_supplicant
    # goes back to picking the strongest BSSID.
    fav = preferred_ssid(nets)
    if fav:
        log.info("Preferred network(s): %s (priority=%d; others get %d) — tried "
                 "first, other saved networks still used as fallback",
                 ", ".join(repr(s) for s in fav), PREFERRED_PRIORITY, DEFAULT_PRIORITY)
    else:
        log.info("No preferred network set (all at priority=%d — wpa_supplicant "
                 "picks by signal strength)", DEFAULT_PRIORITY)
    _stop_all_wpa_supplicant()
    safe_write_wpasupplicant(nets)
    ensure_wpa_supplicant_running()
    reconfigure_wifi()
    enable_all_networks("at daemon start")

    # 3) Initial connect attempt window.
    log.info("Waiting up to %ds for a known WiFi network...", CONNECT_TIMEOUT)
    deadline = time.time() + CONNECT_TIMEOUT
    last_sig = None
    while time.time() < deadline:
        _check_clock_step()
        if is_connected():
            log.info("Connected to WiFi (SSID=%s). Staying alive as supervisor.",
                     connected_ssid())
            break
        sig = wpa_snapshot()
        if sig != last_sig:
            log.info("wifi state: wpa_state=%s ssid=%s ip=%s", *sig)
            last_sig = sig
        time.sleep(POLL_INTERVAL)
    else:
        log.warning("No known WiFi reachable in %ds. Opening setup mode.", CONNECT_TIMEOUT)
        _enter_setup_mode()
        if _setup_mode_loop():
            return 0   # powered off by open-AP expiry
        # A live web-UI reconnect succeeded: continue into supervise() below.

    # 4) Healthy path: keep-alive watchdog (never reboots on its own).
    try:
        supervise()
    except KeyboardInterrupt:
        log.info("Shutting down")
        teardown_ap()
        _flush_logs()
    return 0


def connected_ssid():
    rc, out = run(["iwgetid", "-r", WLAN_IFACE])
    return out.strip() or None


def _readable(path):
    """(ok, reason) for `path` — lets the diagnostic explain itself.

    `os.access` is a lie for root (it reports True even for mode-000 files), so
    only trust it for the negative answer; a positive is confirmed by the
    caller actually opening the file.
    """
    if not os.path.exists(path):
        return False, "missing"
    if not os.access(path, os.R_OK):
        return False, "PermissionError (mode %o, owner %s)" % (
            os.stat(path).st_mode & 0o777, os.stat(path).st_uid)
    return True, ""


def _redact_psk(psk):
    """Shorten a PSK for paste-safe output.

    A 64-hex `psk=` IS the WPA2 master key, so a full value in a terminal
    scrollback / pasted log is equivalent to handing over the WiFi password.
    """
    if not psk:
        return psk
    if "--redact" not in sys.argv:
        return psk
    return psk[:8] + "..." + psk[-4:] if len(psk) > 16 else "***"


def _seen_ssids(nets):
    """Human-readable `ssid (signal)` list from scan results, sorted.

    Extracted so it is unit-testable: scan_networks() returns a list of
    dicts, and iterating one yields its KEYS, so an inline `for a, _ in nets`
    unpacks "bssid" as characters and dies with "too many values to unpack".
    """
    out = []
    for n in nets or []:
        ssid = (n.get("ssid") or "").strip()
        if not ssid:
            continue
        sig = n.get("signal")
        out.append("%s (%s)" % (ssid, sig) if sig is not None else ssid)
    return ", ".join(sorted(set(out)))


def diagnose():
    """Print a read-only snapshot for SSH debugging (never touches the system).

    Run on the Pi as: sudo python3 wifi_setup_daemon.py --diagnose
    Add --redact to shorten PSKs before pasting the output into a shared log.

    Side-effect free by design: no hostapd/dnsmasq/wpa_supplicant control, no
    writes, no DHCP changes.  Separates "PSK mismatch" from "attach/DHCP race"
    in the "associates then vanishes after seconds" case.

    Needs root: known-wifi.json and the generated confs are mode-0600 root-only.
    Without it the run still completes, but the file sections say WHY they are
    empty instead of silently reporting a broken-looking network setup.
    """
    sep = "=" * 60
    print(sep)
    print("WIFI-SETUP DIAGNOSE (read-only)")
    print(sep)

    if hasattr(os, "geteuid") and os.geteuid() != 0:
        print("WARNING: not running as root.  Sections [1] and [2] need it, because")
        print("         these files are mode-0600 root-owned:")
        for p in (STORE_FILE, WPA_CONF, WPA_IFACE_CONF):
            ok, why = _readable(p)
            print("           %-52s %s" % (p, "readable" if ok else "UNREADABLE (%s)" % why))
        print("         Re-run as:  sudo python3 %s --diagnose"
              % os.path.basename(__file__))
        print("         (a store showing '0 networks' here means 'cannot read', not 'empty')")

    store_ok, store_why = _readable(STORE_FILE)
    if store_ok:
        nets = load_wlans()
        print("\n[1] Known networks in %s (%d)" % (STORE_FILE, len(nets)))
        for n in nets:
            psk = n.get("psk") or ""
            pw = n.get("password") or ""
            mgmt = n.get("key_mgmt") or ("WPA2-PSK" if psk else "none")
            print("    ssid=%-20r psk=%s key_mgmt=%s plaintext_password=%s "
                  "preferred=%s priority=%d"
                  % (n.get("ssid"), _redact_psk(psk) or "(none)", mgmt,
                     "yes (%d chars)" % len(pw) if pw else "no",
                     "yes" if n.get("preferred") else "no",
                     _resolve_priority(n)))
        print("    -> compare psk above with:  wpa_passphrase <ssid> '<password>'")
        fav = preferred_ssid(nets)
        print("    -> priority is the ONLY thing wpa_supplicant orders by; the "
              "preferred network is %s"
              % (", ".join(repr(s) for s in fav) if fav
                 else "NOT SET (all networks tie at %d - wpa_supplicant picks "
                      "the strongest signal)" % DEFAULT_PRIORITY))
        print("    -> `wpa_cli list_networks` does NOT show priority, so this is "
              "the only place to check it.")
    else:
        nets = []
        print("\n[1] %s UNREADABLE (%s) — re-run with sudo" % (STORE_FILE, store_why))
        print("    (load_wlans() is NOT called: it swallows the error and returns [],"
              " which reads as an empty store)")

    for p in (WPA_CONF, WPA_IFACE_CONF):
        if not os.path.isfile(p):
            print("\n[2] %s MISSING" % p)
            continue
        try:
            with open(p) as f:
                conf = f.read()
        except OSError as exc:
            print("\n[2] %s exists but UNREADABLE (%s) — re-run with sudo" % (p, exc))
            continue
        # The generator indents block entries ("    psk=..."), so an unstripped
        # startswith() silently matches nothing and reports "(none)".
        head = [l.strip() for l in conf.splitlines() if l.strip().startswith("psk=")]
        prio = [l.strip() for l in conf.splitlines() if l.strip().startswith("priority=")]
        print("\n[2] %s exists (managed marker: %s)" % (
            p, "managed" if wpa_conf_is_managed(p) else "UNMANAGED - preserved lines"))
        print("    psk lines in conf: %s" % (head or "(none)"))
        print("    priority lines in conf: %s" % (prio or "(none)"))

    print("\n[3] wpa_cli status for %s:" % WLAN_IFACE)
    rc, out = run(wpa_cli("status"))
    if rc == 0:
        for line in out.splitlines():
            print("    " + line)
        print("    ip(has_ipv4)=%s route_or_dns=%s" % (
            has_ipv4(WLAN_IFACE),
            _route_via_wlan() or dns_works()))
        print("    is_connected()=%s" % is_connected())
    else:
        print("    wpa_cli could not talk to wpa_supplicant (exit %d)" % rc)

    print("\n[4] country=%s" % _derive_country())
    rc, out = run(["iw", "dev", WLAN_IFACE, "info"])
    print("    iw dev %s info: %s" % (WLAN_IFACE, out.strip().replace("\n", " | ") if rc == 0 else "failed"))

    print("\n[5] Scan (via wifi_scan.py):")
    try:
        import wifi_scan
        nets, note = wifi_scan.scan_networks(WLAN_IFACE)
        print("    note: %s" % (note or "(networks found)"))
        print("    seen: %s" % (_seen_ssids(nets) or "(none)"))
    except Exception as exc:
        print("    scan failed: %r" % exc)


if __name__ == "__main__":
    sys.exit(main())
