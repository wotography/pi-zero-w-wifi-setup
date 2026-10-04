# Roadmap

## Before / around the first public release

- **License.** Not decided yet; until a `LICENSE` file exists, all rights are
  reserved and nobody may legally reuse the code.
- **On-device pass on a fresh install.** Verified so far on the original
  device: captive portal on macOS and iOS, *Leave setup mode*. Still to prove,
  ideally on a freshly flashed Bullseye/Legacy image that has never seen the
  project:
  - installer: NetworkManager refusal, country prompt, generated AP password;
  - hostname-based names (AP, page, `<name>.setup`, certificate regenerated
    after a rename);
  - in-page confirm (Shutdown, Forget), ✓/★ marks in the scan list;
  - captive sheet on **Android**;
  - `[up …]` / *Boot reference* / *Wall clock stepped* log lines;
  - the **dead-hostapd path** (`systemctl stop hostapd` + force setup mode →
    after `WIFI_AP_FAIL_LIMIT` failures the daemon returns to client mode and
    keeps supervising).
- **Crash loops must be visible.** `Restart=on-failure` + `RestartSec=3`
  respawns an unexpected exit silently. Add `StartLimitBurst` /
  `StartLimitIntervalSec` and an explicit `RestartPreventExitStatus`. (Unclean
  restarts without a `SIGTERM received` line were seen once; OOM was ruled out,
  the cause is unknown.)
- **Direct `-B` cold boot.** Prove the daemon's own `wpa_supplicant -B -i wlan0`
  fallback from a cold boot with `wpa_supplicant@wlan0` disabled — the "foreign
  or minimal image" case.

## Next

- **Daemon timers on the monotonic clock.** The connect window, the
  link-down counter, `AP_RESTORE_GRACE` and the open-AP power-off deadline
  compare `time.time()`, so an NTP step in the middle of one shortens or
  stretches it (a +1 h step during an open-AP countdown would power off at
  once). Switch the durations to `time.monotonic()`, keeping the epoch only for
  the page's countdown.
- **NetworkManager backend** for Raspberry Pi OS Bookworm and later (`nmcli`
  for client connections and the hotspot). Today the installer refuses such
  systems.
- **CI**: run `python3 -m unittest discover -s tests` and `bash -n` on every
  push.
- **Installer-side home network.** If the seeded store has exactly one network,
  ask: *"Mark <SSID> as your home network? The device will always try to
  connect to it first, and fall back to your other saved networks when it is
  not available."* (yes/no → `"preferred": true`).
- **Smarter update rescue.** The rescue timer in `docs/DEVELOPMENT.md` blindly
  restarts the daemon: it re-runs the same new code and fires even when all is
  well. Better: act only if the device has neither a client link nor a running
  setup AP, and then restore the previous install from a backup (e.g.
  `/opt/wifi-setup.prev`) before restarting. Mind the timing — two minutes can
  fall inside the connect window plus pre-AP scan.

## Not planned

- **WPA-Enterprise (802.1X)** — a missing feature, not a robustness gap; the UI
  offers WPA2-PSK and open networks only.
