# AGENTS.md

wifi-setup: a WiFi supervisor with a fallback setup access point and setup page
for headless Raspberry Pis (developed on a Pi Zero W, Raspberry Pi OS
Bullseye with dhcpcd + wpa_supplicant). Python 3 standard library only.

## Layout

- `wifi_setup.sh` — installer (root, idempotent). Finds its payload via its
  own directory, so the repository root is the shippable unit.
- `wifi_setup_daemon.py` — supervisor service; generates wpa_supplicant,
  hostapd and dnsmasq confs; `--diagnose [--redact]` is a read-only report.
- `wifi_config_server.py` — stdlib `http.server` setup page + JSON API,
  spawned by the daemon in setup mode.
- `wifi_scan.py` — shared scan/cache helpers and device naming.
- `templates/`, `static/`, `systemd/`, `config/` — copied verbatim by the
  installer. `config/known-wifi.json` must stay empty in the repository.
- `tests/` — `python3 -m unittest discover -s tests`; `docs/` — design,
  troubleshooting, development.

## Rules

- **No new dependencies** (no Flask, no pip packages). Target: the distro's
  Python on a Pi Zero W.
- `known-wifi.json` is the only store; all writes go through `write_store()`.
  Entries are `{ssid, psk}` (64-hex) or `{ssid, password}`; empty password =
  open network; `"preferred": true` is a boolean flag on at most one entry and
  maps to `priority=10` (others `1`) only in the generated conf.
- Generated confs are never shipped as templates.
- The config server binds only to the setup-AP address and must keep its
  Host (DNS-rebinding) and Origin (CSRF) checks on every route; a foreign-host
  `GET` may only ever be answered with a redirect.
- Every action that drops the setup AP answers the HTTP request first.
- Anything touching `systemctl`, `hostapd`, `dnsmasq`, `wpa_cli`, `iw` or `/etc`
  only runs on the Pi as root. Both Python modules import cleanly anywhere and
  read their configuration from the environment at import time (see
  `tests/support.py`).

## Verification

1. `python3 -m unittest discover -s tests -v`
2. `python3 -m py_compile *.py` and `bash -n wifi_setup.sh` (use
   `PYTHONPYCACHEPREFIX` outside the repo or remove `__pycache__/` afterwards).
3. On-device behaviour (boot, AP, systemd) can only be verified on a Pi. On a
   device reachable only over WiFi, follow the update procedure in
   `docs/DEVELOPMENT.md` — every service restart drops the link.

## Docs

`README.md` is for users, `docs/` for details. Record user-visible changes in
`Changelog.md` (under *Unreleased*), open work in `Roadmap.md`.
