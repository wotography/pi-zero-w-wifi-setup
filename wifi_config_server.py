#!/usr/bin/env python3
"""wifi-setup config server.

A tiny, stdlib-only HTTP(S) server (no Flask / no dependencies) that:
  - Serves a single-page UI (templates/index.html + static assets).
  - GET  /api/scan      -> nearby SSIDs from the daemon's client-mode scan
                            cache (live scan only as a fallback).
  - GET  /api/status    -> current known-network state / live SSID.
  - GET  /api/log       -> tail of the daemon's flat log (/api log view).
  - POST /api/connect   -> body {ssid, password, mode, preferred?}
                             mode "live"   = try wpa_cli reconnect, no reboot;
                                             on failure returns a recoverable
                                             error (no auto-reboot).
                             mode "reboot" = save network then reboot the Pi.
                             preferred      = flag this network as the "home
                             network" (wpa_supplicant tries it first).  Omitted
                             = keep the stored flag.
  - POST /api/preferred -> body {ssid, preferred?} mark one saved network as
                           the home network (or clear it).  Stored only: no
                           conf rebuild, no switch — it applies at the next
                           connection or reboot, and disables nothing.
  - POST /api/forget    -> body {ssid} drop a saved network from the store.
  - POST /api/normal    -> leave setup mode: tear down the AP, hand wlan0
                           back to wpa_supplicant, reconnect to a saved
                           network (no reboot).
  - POST /api/rescan    -> ask the daemon to pause the AP, rescan in client
                           mode and bring the AP back up (the radio can't
                           scan while the AP owns it).
  - POST /api/shutdown  -> answer, then power the device off (the page's
                           "Shutdown <hostname>" menu entry).

Runs on the setup AP (192.168.4.1) so a phone/laptop can configure the device.

Captive portal: the daemon's dnsmasq answers every DNS name with the AP IP, so
a phone's connectivity probe (captive.apple.com, connectivitycheck.gstatic.com,
...) lands here.  A GET with a foreign Host header is answered with a redirect
to the setup page — never with content — so the phone opens its captive-portal
sheet while the DNS-rebinding guard stays intact.  WIFI_CAPTIVE_PORTAL=0 turns
the redirect off (foreign hosts get 403 again).

Security:
  - The server binds ONLY to the AP address, never to all interfaces.
  - The API is unauthenticated by design, but reachable only from devices on
    the setup AP (which is WPA2-protected unless the open-AP option was chosen).
  - The UI sends JSON POSTs; a cross-origin browser cannot read responses
    (no CORS headers) and the POST triggers a CORS preflight that is refused.
  - An OPEN AP reports `seconds_left` and the page shows a countdown until the
    daemon powers the device off (it auto-powers-off to avoid a lingering AP).
"""

import hashlib
import html
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import wifi_scan

# --------------------------------------------------------------------------
# Shared with the daemon
# --------------------------------------------------------------------------
CFG_DIR    = os.environ.get("WIFI_CFG_DIR", "/etc/wifi-setup")
WPA_CONF   = os.environ.get("WIFI_WPA_CONF", "/etc/wpa_supplicant/wpa_supplicant.conf")
WLAN_IFACE = os.environ.get("WIFI_IFACE", "wlan0")
LOG_FILE   = os.environ.get("WIFI_LOG_FILE", os.path.join(CFG_DIR, "daemon.log"))


def _pick_store():
    """Canonical known-wifi.json store only (WIFI_STORE_FILE / env override)."""
    cfg = os.environ.get("WIFI_CFG_DIR", "/etc/wifi-setup")
    if os.environ.get("WIFI_STORE_FILE"):
        return os.environ["WIFI_STORE_FILE"]
    return os.path.join(cfg, "known-wifi.json")


STORE_FILE = os.environ.get("WIFI_STORE_FILE") or _pick_store()

# Scan results cached by the daemon while wlan0 is still in client mode (the
# Pi Zero W's brcmfmac can't scan while the setup AP owns the radio).  We
# serve this cache; a live scan is only a fallback (e.g. local testing).
SCAN_CACHE_FILE = os.environ.get("WIFI_SCAN_CACHE",
                                 os.path.join(CFG_DIR, "scan-cache.json"))

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
TEMPLATES  = os.path.join(BASE_DIR, "templates")
STATIC     = os.path.join(BASE_DIR, "static")


def run(cmd, timeout=30):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as e:
        return -1, str(e)


def load_wlans():
    if not os.path.isfile(STORE_FILE):
        return []
    try:
        with open(STORE_FILE) as f:
            data = json.load(f)
        nets = data.get("networks", []) if isinstance(data, dict) else data
        return [n for n in nets if n.get("ssid")]
    except Exception:
        return []


def wpa2_psk(ssid, password):
    """Derive the 64-hex WPA2-PSK from a passphrase (PBKDF2-HMAC-SHA1).

    Identical to `wpa_passphrase`, but pure stdlib: no subprocess, and the
    plaintext passphrase is never written to disk or echoed anywhere.
    """
    return hashlib.pbkdf2_hmac(
        "sha1", password.encode("utf-8"), ssid.encode("utf-8"), 4096, 32).hex()


def _as_bool(value, default=True):
    """JSON truthiness that survives a hand-rolled client sending "false"."""
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "no", "off")
    return bool(value)


def write_store(nets):
    """Persist the network list atomically, mode 0600. The single store writer.

    The store holds PSK hashes and (for hand-edited entries) plaintext
    passwords, so it must never be readable by anyone but root, and it must
    never be seen half-written: write a sibling temp file, chmod it *before* the
    rename (so the content is never briefly world-readable), then os.replace()
    it into place — atomic within the filesystem.  Every mutating path
    (save / preferred / forget) goes through here, so the 0600 guarantee has
    exactly one place that can forget it.
    """
    os.makedirs(os.path.dirname(STORE_FILE) or ".", exist_ok=True)
    tmp = STORE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"networks": nets}, f, indent=2)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, STORE_FILE)
    try:
        os.chmod(STORE_FILE, 0o600)
    except OSError:
        pass
    return nets


def save_network(ssid, password, preferred=None):
    """Store one network's credential, keeping any `preferred` flag it has.

    Re-saving an already-saved network (a new password) must not silently drop
    its star, so the entry is *updated* rather than rebuilt from scratch.  Only
    the credential key that applies is kept: a secured entry is `{ssid, psk}`
    and an open one `{ssid, password: ""}`, never both — a stale leftover
    would make the generator pick the wrong one.

    `preferred` is tri-state: None = leave the stored flag alone, True/False =
    set it explicitly (the web UI's home-network checkbox).
    """
    nets = load_wlans()
    existing = next((n for n in nets if n.get("ssid") == ssid), None)
    if not password and existing and (existing.get("psk") or existing.get("password")):
        # Re-selecting an already-saved secured network with an empty password
        # means "reconnect to it", not "turn it into an open network".  Keep
        # the stored credential so a working psk isn't blanked.  (The
        # `preferred` flag, if any, comes back unchanged too.)
        if preferred is not None and bool(existing.get("preferred")) != bool(preferred):
            if preferred:
                existing["preferred"] = True
            else:
                existing.pop("preferred", None)
            write_store(nets)
        return nets
    nets = [n for n in nets if n.get("ssid") != ssid]  # de-dup, keep newest order
    entry = dict(existing or {})   # keep unknown/extra keys (e.g. preferred)
    entry["ssid"] = ssid
    entry.pop("psk", None)
    entry.pop("password", None)
    # Persist the precomputed PSK hash instead of the plaintext passphrase, so
    # the store never holds a reusable WPA passphrase.  An empty password means
    # an open network and is stored as a plain marker.
    if password:
        entry["psk"] = wpa2_psk(ssid, password)
    else:
        entry["password"] = ""
    if preferred is None:
        pass          # leave the flag exactly as it was (or absent)
    elif preferred:
        entry["preferred"] = True
    else:
        entry.pop("preferred", None)   # only ever store the positive flag
    nets.append(entry)
    return write_store(nets)


def set_preferred(ssid, wanted=True):
    """Flag one network as the preferred (home) network; clear the flag on all
    others.  Returns the updated list, or None when `ssid` is not saved.

    Promotion is permanent and disables nothing: it only decides which network
    wpa_supplicant tries *first*, and the generated conf is rebuilt on the next
    connection attempt or reboot.
    """
    nets = load_wlans()
    if not any(n.get("ssid") == ssid for n in nets):
        return None
    for n in nets:
        if n.get("ssid") == ssid:
            if wanted:
                n["preferred"] = True
            else:
                n.pop("preferred", None)
        elif n.get("preferred"):
            n.pop("preferred", None)   # a single favourite, not a ranking
    return write_store(nets)


def delete_network(ssid):
    """Forget one saved network by SSID.  Returns the remaining list.

    Deleting the preferred entry simply leaves nothing preferred, which
    degrades safely back to signal-strength ordering — no special case needed.
    """
    nets = [n for n in load_wlans() if n.get("ssid") != ssid]
    return write_store(nets)


def public_wlans():
    """Store networks with password/psk redacted (the UI never needs them)."""
    return [{k: v for k, v in n.items() if k not in ("password", "psk")}
            for n in load_wlans()]


def cached_scan():
    """Return (networks, cached_flag, scanned_at) for the setup page.

    Prefer the daemon's cache (captured in client mode); fall back to a live
    scan only when no cache exists (e.g. a Mac smoke test / manual run).
    """
    entry = wifi_scan.read_cache(SCAN_CACHE_FILE)
    if entry is not None:
        nets = entry.get("networks", []) or []
        return nets, True, entry.get("at")
    networks, _note = wifi_scan.scan_networks(WLAN_IFACE)
    return networks, False, None


class Handler(BaseHTTPRequestHandler):

    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_page(self):
        """index.html with the device name filled in (title, header, labels).

        Rendered server-side rather than patched in by JS, so the right name
        is there on first paint and without JavaScript.
        """
        try:
            with open(os.path.join(TEMPLATES, "index.html"), encoding="utf-8") as f:
                page = f.read()
        except OSError:
            self.send_error(404)
            return
        for key, value in (("{{DEVICE_NAME}}", DEVICE_NAME), ("{{AP_SSID}}", AP_SSID)):
            page = page.replace(key, html.escape(value))
        body = page.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path, content_type):
        try:
            with open(path, "rb") as f:
                body = f.read()
        except OSError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _host_ok(self):
        """Reject requests whose Host header isn't one of ours (DNS rebinding)."""
        host = (self.headers.get("Host") or "").strip()
        if not host:
            return True
        name = host.split(":")[0].strip("[]").lower()
        return name in _ALLOWED_HOSTS

    def _origin_ok(self):
        """Reject state-changing requests from foreign origins (CSRF).

        Browsers attach an Origin header to cross-origin POSTs; a legitimate
        request comes from the page itself (192.168.4.1 / <name>.setup)
        or has no Origin at all (curl, scripting).
        """
        origin = self.headers.get("Origin")
        if not origin:
            return True
        try:
            p = urlparse(origin)
        except ValueError:
            return False
        if p.scheme not in ("http", "https"):
            return False
        return (p.hostname or "").lower() in _ALLOWED_HOSTS

    def _send_portal_redirect(self):
        """Captive-portal answer for a foreign Host: redirect, no content."""
        self.send_response(302)
        self.send_header("Location", _portal_url())
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        if not self._host_ok():
            # A phone's connectivity probe (wildcard DNS sends every name
            # here): point it at the setup page so the captive sheet opens.
            if CAPTIVE_PORTAL:
                self._send_portal_redirect()
            else:
                self.send_error(403)
            return
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/" or path == "/index.html":
            self._send_page()
        elif path == "/api/scan":
            nets, cached, at = cached_scan()
            self._send_json({"ok": True, "cached": cached, "at": at,
                             "known": [n["ssid"] for n in load_wlans()],
                             "networks": sorted(
                                 nets,
                                 key=lambda n: n.get("signal") or -200,
                                 reverse=True)})
        elif path == "/api/status":
            self._send_json({
                "ok": True,
                "networks": public_wlans(),   # passwords/psk redacted
                "ssid": (run(["iwgetid", "-r", WLAN_IFACE])[1]).strip(),
                "device_name": DEVICE_NAME,
                "ap_ssid": AP_SSID,
                "secured": bool(os.environ.get("WIFI_AP_PASSWORD")),
                "seconds_left": _seconds_left(),
            })
        elif path == "/api/log":
            self._send_json({"ok": True, "log": _tail_log()})
        elif path.startswith("/static/"):
            name = os.path.basename(path)
            full = os.path.join(STATIC, name)
            ctype = ("application/javascript" if name.endswith(".js")
                     else "text/css" if name.endswith(".css")
                     else "application/octet-stream")
            self._send_file(full, ctype)
        else:
            self.send_error(404)

    def do_POST(self):
        if not self._host_ok():
            self.send_error(403)
            return
        if not self._origin_ok():
            self._send_json({"ok": False, "error": "forbidden origin"}, 403)
            return
        parsed = urlparse(self.path)
        # One body parse for every route, before the dispatch: the store
        # mutators (connect / preferred / forget) all take {"ssid": ...}.
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        ssid = str(payload.get("ssid") or "").strip()
        if parsed.path == "/api/preferred":
            # Star toggle: promote one network to "tried first".  Stored only —
            # deliberately no conf rebuild and no connect, so clicking it can
            # never drop the link the user is configuring through.
            if not ssid:
                self._send_json({"ok": False, "error": "missing ssid"}, 400)
                return
            wanted = _as_bool(payload.get("preferred"), True)
            nets = set_preferred(ssid, wanted)
            if nets is None:
                self._send_json({"ok": False,
                                 "error": "network '%s' is not saved" % ssid}, 400)
                return
            self._send_json({
                "ok": True, "ssid": ssid, "preferred": wanted,
                "networks": public_wlans(),
                "message": ("'%s' is now the home network — it is tried first "
                            "every time the device connects." % ssid) if wanted
                           else ("'%s' is no longer preferred." % ssid),
                "note": ("Saved. Takes effect the next time the device connects "
                         "(or on the next reboot)."),
            })
            return
        if parsed.path == "/api/forget":
            if not ssid:
                self._send_json({"ok": False, "error": "missing ssid"}, 400)
                return
            known = any(n.get("ssid") == ssid for n in load_wlans())
            if not known:
                self._send_json({"ok": False,
                                 "error": "network '%s' is not saved" % ssid}, 400)
                return
            nets = delete_network(ssid)
            self._send_json({
                "ok": True, "ssid": ssid, "networks": public_wlans(),
                "message": ("'%s' forgotten — the device will no longer try "
                            "it. %d network(s) still saved."
                            % (ssid, len(nets))),
            })
            return
        if parsed.path == "/api/normal":
            # Manual "back to normal mode": only useful when a saved network
            # exists (nothing to reconnect to otherwise).
            if not load_wlans():
                self._send_json({"ok": False, "error": "no known networks saved"}, 400)
                return
            self._send_json({"ok": True, "mode": "normal",
                             "message": "Leaving setup mode; reconnecting to a saved network."})
            try:
                import wifi_setup_daemon as d
                d.mark_exit_setup()
            except Exception:
                pass
            return
        if parsed.path == "/api/rescan":
            # Ask the daemon to pause the AP, scan in client mode and bring
            # the AP back up.  The response is sent before the daemon acts
            # (it polls the connect-state file), so the phone gets it.
            self._send_json({
                "ok": True, "rescan": True,
                "message": ("Restarting the setup AP to scan; rejoin "
                            "'%s' in a few seconds." % AP_SSID),
            })
            try:
                import wifi_setup_daemon as d
                d.mark_rescan()
            except Exception:
                pass
            return
        if parsed.path == "/api/shutdown":
            # Answer first: the setup AP (and this socket) disappears with the
            # power-off, so the page must already have its message.
            host = DEVICE_NAME
            self._send_json({
                "ok": True, "shutdown": True, "device_name": host,
                "message": ("Shutting down %s now. It stays off until its "
                            "power is unplugged and plugged back in." % host),
            })
            threading.Timer(1.0, _shutdown).start()
            return
        if parsed.path != "/api/connect":
            self.send_error(404)
            return
        password = str(payload.get("password") or "").strip()
        mode = (payload.get("mode") or "live").strip()
        if not ssid:
            self._send_json({"ok": False, "error": "missing ssid"}, 400)
            return
        if password and not (8 <= len(password) <= 63):
            self._send_json({
                "ok": False,
                "error": ("password must be 8-63 characters, "
                          "or empty for an open network"),
            }, 400)
            return
        # Re-selecting an already-saved secured network with an empty password
        # keeps its stored credential (see save_network) — say so.
        kept = (not password and any(
            n.get("ssid") == ssid and (n.get("psk") or n.get("password"))
            for n in load_wlans()))
        # Tri-state: only the "home network" checkbox writes a flag; an absent
        # key leaves whatever the star already is (see save_network).
        pref = payload.get("preferred")
        save_network(ssid, password, None if pref is None else _as_bool(pref, True))

        # Read the log BEFORE answering: the response itself must carry the
        # daemon log tail because the web UI's connection to the setup AP dies
        # the moment we switch (live) or reboot — the page wouldn't get it
        # otherwise ("the daemon log box is empty" bug).
        log_tail = _tail_log()

        if mode == "reboot":
            # Explicit reboot request: answer first, then reboot.
            self._send_json({
                "ok": True,
                "mode": "reboot",
                "ssid": ssid,
                "log_tail": log_tail,
                "message": ("Password accepted. SSID '%s' saved%s. Rebooting "
                            "now — after the restart the device connects to "
                            "'%s'. This page comes back only if no network "
                            "connects."
                            % (ssid, "" if kept else " (new)", ssid)),
            })
            threading.Timer(1.0, _reboot).start()
            return

        # Default "live": answer FIRST, then switch WITHOUT rebooting.  The
        # moment connect_now tears the setup AP down, this socket dies — so we
        # must send the response (message + log tail) before triggering it.
        self._send_json({
            "ok": True,
            "mode": "live",
            "ssid": ssid,
            "log_tail": log_tail,
            "message": ("Password accepted. SSID '%s' saved%s. Switching to "
                        "'%s' now (no reboot) — the setup AP is going down and "
                        "the device reconnects on its own. This page comes "
                        "back only if no network connects."
                        % (ssid, "" if kept else " (new)", ssid)),
        })
        threading.Timer(1.0, _live_switch, [ssid]).start()
        return

    def log_message(self, *args):
        pass


def _live_switch(ssid):
    """Trigger the daemon's no-reboot wpa_cli switch asynchronously.

    Runs in a background thread AFTER the /api/connect response has been sent
    (the setup AP dies as soon as teardown_ap() runs, so the response — with
    its message and log tail — must already be on its way to the phone).
    """
    try:
        import wifi_setup_daemon as d
        d.connect_now(ssid, "")
    except Exception:
        pass


def _seconds_left():
    """Seconds until the daemon power-off for an open setup AP (or None).

    The daemon hands us the absolute expiry via WIFI_SETUP_EXPIRES_AT env when
    the AP has no password; a secured AP never sets it.
    """
    v = os.environ.get("WIFI_SETUP_EXPIRES_AT")
    if not v:
        return None
    try:
        return max(0, int(float(v)) - int(time.time()))
    except (ValueError, TypeError):
        return None


def _tail_log(n=80):
    """Return the last n lines of the daemon's flat log (for the web UI)."""
    if not os.path.isfile(LOG_FILE):
        return []
    try:
        with open(LOG_FILE) as f:
            lines = f.read().splitlines()
        return lines[-n:]
    except Exception:
        return []


def _reboot():
    run(["systemctl", "stop", "hostapd"])
    run(["systemctl", "stop", "dnsmasq"])
    run(["reboot"], timeout=5)


def _shutdown():
    # systemctl only — deliberately no `shutdown -h now` fallback: that binary
    # exists on a Mac, so a local smoke test of /api/shutdown would power the
    # development machine off.  The daemon's SIGTERM handler tears down the
    # rest on its way out.
    run(["systemctl", "stop", "hostapd"])
    run(["systemctl", "stop", "dnsmasq"])
    run(["systemctl", "poweroff"], timeout=10)


def _make_server():
    global _server
    server = ThreadingHTTPServer((CONFIG_HOST, _PORT), Handler)
    cert = os.environ.get("WIFI_CERT")
    key = os.environ.get("WIFI_KEY")
    if cert and key and os.path.isfile(cert) and os.path.isfile(key):
        import ssl
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=cert, keyfile=key)
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
    return server


# Bind ONLY to the setup AP address (192.168.4.1), never 0.0.0.0, so the web
# UI is unreachable from every other interface (ethernet, USB gadget, ...).
# The daemon overrides this via WIFI_CONFIG_HOST when it spawns us; e.g. for
# local smoke tests use WIFI_CONFIG_HOST=127.0.0.1.
CONFIG_HOST = os.environ.get("WIFI_CONFIG_HOST", "192.168.4.1")
# Names shown to the user — the daemon passes them in (WIFI_DEVICE_NAME /
# WIFI_AP_SSID / WIFI_SETUP_DOMAIN); a standalone run derives the same ones.
DEVICE_NAME = wifi_scan.device_name()
AP_SSID = wifi_scan.ap_ssid()
SETUP_DOMAIN = wifi_scan.setup_domain()
_ALLOWED_HOSTS = {CONFIG_HOST.lower(), SETUP_DOMAIN, "localhost", "127.0.0.1"}
_PORT = int(os.environ.get("WIFI_CONFIG_PORT", sys.argv[1] if len(sys.argv) > 1 else "80"))
CAPTIVE_PORTAL = os.environ.get("WIFI_CAPTIVE_PORTAL", "1").strip() != "0"


def _portal_url():
    """Absolute URL of the setup page (captive-portal redirect target)."""
    scheme = "https" if os.environ.get("WIFI_CERT") else "http"
    default = 443 if scheme == "https" else 80
    port = "" if _PORT == default else ":%d" % _PORT
    return "%s://%s%s/" % (scheme, CONFIG_HOST, port)


def _start_redirect_server():
    """Plain-HTTP listener that 301-redirects to the HTTPS port.

    When TLS is active the config server only listens on the HTTPS port, so
    typing http://192.168.4.1 would otherwise produce a raw connection error.
    A tiny second listener on WIFI_REDIRECT_HTTP_PORT (default 80) bounces the
    user to https://.  Needs root to bind port 80; if that fails we carry on —
    the HTTPS endpoint is unaffected.
    """
    if not os.environ.get("WIFI_CERT"):
        return
    try:
        port = int(os.environ.get("WIFI_REDIRECT_HTTP_PORT", "80"))
    except ValueError:
        return

    class _RedirectHandler(BaseHTTPRequestHandler):
        def _redirect(self):
            path = urlparse(self.path).path or "/"
            status = 301
            host = (self.headers.get("Host") or "").split(":")[0].strip("[]").lower()
            if CAPTIVE_PORTAL and host and host not in _ALLOWED_HOSTS:
                # Captive-portal probe (e.g. /hotspot-detect.html on a foreign
                # host): its path doesn't exist here, send it to the page root
                # with a temporary redirect the OS must not cache.
                path = "/"
                status = 302
            self.send_response(status)
            self.send_header("Location", "https://%s:%d%s" % (CONFIG_HOST, _PORT, path))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_GET = do_HEAD = do_POST = do_PUT = do_DELETE = _redirect

        def log_message(self, *args):
            pass

    try:
        srv = ThreadingHTTPServer((CONFIG_HOST, port), _RedirectHandler)
    except OSError as e:
        print("Could not start HTTP->HTTPS redirect on %s:%d (%s)"
              % (CONFIG_HOST, port, e), flush=True)
        return
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    print("Redirecting http://%s:%d -> https://%s:%d" % (CONFIG_HOST, port, CONFIG_HOST, _PORT),
          flush=True)
    return srv


def main():
    server = _make_server()
    _start_redirect_server()
    scheme = "https" if os.environ.get("WIFI_CERT") else "http"
    print("Config server on %s://%s:%d" % (scheme, CONFIG_HOST, server.server_address[1]), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
