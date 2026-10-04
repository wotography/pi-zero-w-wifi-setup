# Changelog

wifi-setup began as the WiFi layer of a single headless Pi Zero W project and
was later split out as a standalone tool. Versions before the public release
were internal snapshots; their entries are condensed here to what is still
useful for understanding the code.

## Unreleased — first public release

**Renamed and made device-neutral.**
- Tool name **wifi-setup** everywhere internally: `/opt/wifi-setup`,
  `/etc/wifi-setup` (`wifi-setup.env`), service `wifi-setup.service`,
  `/etc/dnsmasq.d/wifi-setup.conf`, marker `WIFI-SETUP-MANAGED` in the
  generated `wpa_supplicant.conf`.
- Everything user-visible follows the **hostname**: page title/header/menu,
  setup AP `<name>-Setup` (cut to the 32-byte SSID limit), local address
  `<name>.setup`. Overrides: `WIFI_DEVICE_NAME`, `WIFI_AP_SSID`,
  `WIFI_SETUP_DOMAIN`. Names are derived once in `wifi_scan.py` and handed from
  the daemon to the config server; the page is rendered server-side with the
  name HTML-escaped. `/api/status` reports `device_name` and `ap_ssid`.
- The self-signed certificate is issued for `<name>.setup` with a SAN
  (`DNS:<name>.setup`, `IP:192.168.4.1`) and regenerated after a rename; a
  user-supplied `WIFI_CERT` is never touched.

**Installer.**
- Refuses to run when **NetworkManager** is active (Bookworm default) — the
  tool supports dhcpcd + wpa_supplicant only — and when not run as root; warns
  when `dhcpcd` is missing. Nothing is changed in either case.
- **WiFi country**: taken from `WIFI_COUNTRY`, an existing wpa_supplicant conf
  or `/etc/default/crda`, otherwise asked interactively and written to the env
  file. The daemon logs a WARNING when it has to fall back to its default.
- Persists `WIFI_DEVICE_NAME`; the seed store `config/known-wifi.json` ships
  empty.

**Setup page fixes.**
- Shutdown and Forget confirm in an in-page dialog — the macOS/iOS captive
  sheet silently suppresses `window.confirm()`, so both did nothing there.
- Removed the **"Scan for networks"** button: it never scanned, it only
  reloaded the list recorded before the AP started — which the page already
  loads by itself and refreshes after a *Rescan*. The logic stays wired as
  `reloadLastScan()` (optional `#reload-scan-btn`, "Reload last scan results");
  a failed load now shows a *Try again* link. `tests/test_ui.py` checks that
  every element `app.js` requires exists in `index.html`.
- ✓ (saved) / ★ (home) sit next to the network name instead of inside the
  ellipsis-truncated name, so they are never cut off; compact security labels
  (`WPA2`, `WPA3`, …).

**Setup-AP password is ASCII-only.** WPA2 defines the passphrase as 8–63
printable ASCII characters; the installer's generator still included `€`,
which hostapd accepts but many phones cannot type or hash identically — the
setup AP then rejected the "correct" password. The generator now uses ASCII
only (and no quotes), an own password is validated at install (interactive or
`WIFI_AP_PASSWORD`), and the daemon warns at start about an existing invalid
password. Covered by `tests/test_ap_password.py`, which checks the installer's
shell functions against the daemon's rule.

**Project.**
- Automated tests in `tests/` (stdlib `unittest`, run anywhere): store, conf
  generation, dnsmasq conf, logging helpers, naming, and the HTTP layer
  in-process (captive redirect, Host/Origin guards, rendering, shutdown).
- Documentation split into `README.md` (users) and `docs/` (design,
  troubleshooting, development).

## 0.10 — 2026-10-03

- **Home-network checkbox off by default**; pre-ticked only for the network
  that already is the home network, so re-saving it keeps the star.
- **Captive portal**: dnsmasq resolves every name to the AP; the config server
  answers a foreign-host `GET` with a `302` to the page (never content), keeps
  `403` for foreign-host/-origin `POST`s, and the HTTP→HTTPS listener sends
  probes to `/`. `WIFI_CAPTIVE_PORTAL=0` turns it off. HTTPS stays on (the sheet
  shows the self-signed-certificate warning once).
- **☰ menu** with *Leave setup mode & reconnect* and *Shutdown &lt;name&gt;*
  (`POST /api/shutdown`: answer first, then `systemctl poweroff`).
- **No-RTC logging**: `[up H:MM:SS]` on every line, a *Boot reference* line
  (boot time, uptime, `boot_id`, NTP state) and a *Wall clock stepped* line when
  NTP corrects the clock. The service deliberately does not wait for NTP — it
  would block the very network bring-up NTP depends on.

## 0.9 — 2026-09-27

- **Home network (`priority=`).** With no `priority=` in the conf,
  wpa_supplicant joins the strongest BSSID; with two overlapping networks of
  similar strength the device kept landing on the one without the services it
  needed. One store entry can now be `"preferred": true` → `priority=10`, all
  others `priority=1`, emitted on every block. Stored as a boolean, never an
  integer; disables nothing. Startup log and `--diagnose` show the effective
  priorities.
- **Saved networks on the setup page**: ★ toggle and *Forget*
  (`POST /api/preferred`, `POST /api/forget`, behind the Host/Origin guards).
  Both only write the store and take effect at the next connection.
- **One store writer** (`write_store()`: temp file, chmod 0600 *before*
  `os.replace`). `save_network()` updates entries instead of rebuilding them, so
  a re-save keeps the star (`preferred` is tri-state); a secured entry is
  `{ssid, psk}`, an open one `{ssid, password: ""}`, never both.
- Installer removes old `templates/`/`static/` before copying, so files deleted
  upstream cannot be served from a previous install.

## 0.8 — 2026-09-27

**"Saved a second network, came back, and the device never reconnected."**
Two defects in the live-switch path:
- `wpa_cli select_network` (used by *Save & connect*) disables all other
  networks and auto-reconnect, and the generated conf had `update_config=1`, so
  wpa_supplicant persisted `disabled=1`. Fix: `enable_network all` after every
  live switch, after leaving setup mode and at start; the generated conf now
  **forces `update_config=0`**, also when an existing head is preserved.
- The conf was written while a wpa_supplicant was still running, which saved
  its stale state back on exit. Fix: stop every wpa_supplicant **before** each
  conf write.

Also:
- **Self-heal once per outage** before the setup AP: `enable_network all` +
  `reassociate` about a third into the timeout, so other saved networks in range
  are used. If wpa_supplicant itself is dead (no control socket), it is
  restarted first — measured recovery ~34 s without the setup AP.
- **One wpa_supplicant owns `wlan0` at boot**: the installer disables the
  monolithic unit once `wpa_supplicant@wlan0` is enabled; the service unit
  orders after the `@wlan0` unit instead of pulling the monolithic one back in.
- **A broken hostapd is no longer a silent brick**: `setup_ap()` verifies
  dnsmasq + hostapd + AP mode, logs the hostapd journal on failure, and after
  `WIFI_AP_FAIL_LIMIT` failures returns to client mode while supervising on.
- **`--diagnose` fixed and hardened**: PSK lines in the conf are found (they are
  indented), the scan section works, unreadable files are reported as such
  instead of "0 networks", one failing section no longer aborts the rest, root
  requirement documented; `--redact` shortens PSKs for sharing.
- The attach verifies the running instance actually has networks.

## 0.7 — 2026-09-26

- Removed unused hostapd/dnsmasq conf templates (the daemon generates both; the
  dnsmasq sample used `bind-interfaces`, which fails on dnsmasq 2.86+).
- Installer enables `wpa_supplicant@wlan0`, the verified per-interface attach
  path (tolerant when the unit is missing).

## 0.6 — 2026-09-24

- **wpa_supplicant attach on Raspberry Pi OS Bullseye**: `/var/run/wpa_supplicant`
  is created on every attach (tmpfs; some images lack the tmpfiles rule, and
  without the directory no control socket appears), and the attach order is
  `wpa_supplicant@wlan0` → direct `wpa_supplicant -B -i wlan0` → monolithic
  service last (it runs without `-i` and never binds the interface). Each
  candidate must expose the socket **and** answer `PING`. All `wpa_cli` calls
  pin the control directory. Verified across a real reboot.
- Guarded conf writes; offline-safe installer; *Leave setup mode & reconnect*;
  `dhcpcd` paused during setup mode (crash-recoverable marker); one
  deterministic attach path; hostapd/dnsmasq enabled-state never changed.
- PSK-only store entries; the UI stores only the precomputed PSK; re-selecting a
  saved network with an empty password keeps its credential.
- Client-mode scan cache + *Rescan* (the radio cannot scan as an AP); scans stop
  wpa_supplicant first, retry `iw scan flush`, fall back to `wpa_cli`, and log a
  reason for 0 results.
- Regulatory country from env / existing conf / crda instead of hard-coded.
- Connectivity requires `wpa_state=COMPLETED` plus route or DNS (no ICMP); stale
  `wlan0` addresses are flushed at start.
- Responses (message + log tail) are sent before the AP goes down.
- dnsmasq: `bind-dynamic` + `dhcp-authoritative`; hostapd and dnsmasq are
  verified active (one retry).
- Live switches log every `wpa_state` sample; read-only `--diagnose`.

## 0.1 – 0.5 — 2026-09

Initial versions: supervisor daemon with fallback AP, stdlib config server,
installer, known-networks store.
