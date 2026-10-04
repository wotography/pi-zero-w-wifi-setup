#!/usr/bin/env bash
# wifi-setup installer.
#
# One-shot bootstrap.  Installs only what's needed, copies files into place,
# creates systemd units, and pre-seeds the known-networks store.
#
# Run as root on a Raspberry Pi (tested: Pi Zero W, Raspberry Pi OS Bullseye):
#   sudo bash wifi_setup.sh            # interactive: asks about AP security
#   sudo WIFI_AP_PASSWORD="..." bash wifi_setup.sh   # non-interactive (already choosen)
#
# Requirements: internet access (or an existing store under /etc/wifi-setup).
#
# Optional env vars (passed at install time and written to an env file the
# systemd unit loads):
#   WIFI_AP_PASSWORD   password for the setup AP itself.  When unset you are
#                      asked at install whether to auto-generate a strong one,
#                      enter a custom one, or run the AP OPEN (insecure; the
#                      device then auto-powers-off after 15 min).
#   WIFI_DEVICE_NAME   name shown on the setup page and used for the AP /
#                      local domain (default: this machine's hostname)
#   WIFI_AP_SSID       setup AP SSID (default "<hostname>-Setup", max 32 bytes)
#   WIFI_AP_CHANNEL    setup AP channel (default 6)
#   WIFI_CONFIG_PORT   config server port (default 80, or 443 if a cert exists)
#   WIFI_COUNTRY       WiFi regulatory country, e.g. DE (default: taken from an
#                      existing wpa_supplicant conf / crda, else asked)

set -euo pipefail
# Predictable locale for the text tools below.
export LC_ALL="${LC_ALL:-C.UTF-8}"

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST_DIR="/opt/wifi-setup"
CFG_DIR="/etc/wifi-setup"
ENV_FILE="$CFG_DIR/wifi-setup.env"
SERVICE="wifi-setup.service"

# Canonical known-networks store.
STORE_FILE="$CFG_DIR/known-wifi.json"

# Character set for the auto-generated AP password: capitals, small letters,
# digits and a simple set of symbols (no spaces / quotes).  ASCII ONLY: a WPA2
# passphrase must consist of printable ASCII characters — see
# ap_password_problem().
AP_CHARS='ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789!#$%&()*+,-./:;<>?@[]_'
AP_GEN_LEN=16

random_ap_password() {
    local out="" i byte idx
    local -a chars
    # read -a works on bash 3.2+ (mapfile is bash 4+ only).
    read -r -d '' -a chars < <(printf '%s' "$AP_CHARS" | grep -o .)
    for ((i = 0; i < AP_GEN_LEN; i++)); do
        byte=$(od -An -N2 -tu2 /dev/urandom)
        idx=$((byte % ${#chars[@]}))
        out+="${chars[$idx]}"
    done
    printf '%s' "$out"
}

ap_password_problem() {
    # Prints why $1 is not a usable setup-AP password (nothing when it is).
    # WPA2 (IEEE 802.11i) defines the passphrase as 8-63 PRINTABLE ASCII
    # characters.  Anything else — "€", umlauts, emoji — is undefined: hostapd
    # accepts the UTF-8 bytes, but phones may refuse the input or derive a
    # different key, so joining the setup AP fails with the "right" password.
    local p="$1"
    if LC_ALL=C grep -q '[^ -~]' <<<"$p"; then
        echo "use plain ASCII only: letters, digits, spaces and symbols like !#%&*+-./:;?@_ (no umlauts, no €)"
    elif ((${#p} < 8 || ${#p} > 63)); then
        echo "must be 8-63 characters long (it has ${#p})"
    fi
}

ask_ap_security() {
    # Offers three choices:
    #   1) auto-generated strong password
    #   2) custom password (min 8 chars, strong password encouraged)
    #   3) OPEN AP (insecure) -> warning + the daemon auto-powers-off in 15 min
    # Non-interactive (no TTY): fall back to auto-generating a password.
    if [[ ! -t 0 ]]; then
        AP_PASS="$(random_ap_password)"
        echo "    (non-interactive) setup AP password auto-generated and stored in $ENV_FILE"
        echo "    (read it with:  sudo grep WIFI_AP_PASSWORD $ENV_FILE)"
        return
    fi
    local choice p1 p2 confirm problem
    while true; do
        echo ""
        echo "==> Setup AP security"
        echo "    1) Auto-generate a strong password (recommended)"
        echo "    2) Enter my own password (8-63 ASCII chars; strong encouraged)"
        echo "    3) No password (INSECURE: open AP, auto-powers-off after 15 min)"
        printf "    Choose [1/2/3]: "
        read -r choice
        case "$choice" in
            1)
                AP_PASS="$(random_ap_password)"
                echo "    Generated setup AP password: $AP_PASS"
                echo "    (stored in $ENV_FILE; you can change it later)"
                return
                ;;
            2)
                while true; do
                    printf "    AP password (8-63 ASCII chars): "
                    read -rs p1; echo
                    problem="$(ap_password_problem "$p1")"
                    if [[ -n "$problem" ]]; then
                        echo "    Not usable: $problem"
                        continue
                    fi
                    # Encourage a stronger choice without enforcing more types.
                    if ! grep -qE '[^A-Za-z0-9]' <<<"$p1" ||
                       ! grep -qE '[0-9]' <<<"$p1" ||
                       ! grep -qE '[a-z]' <<<"$p1" ||
                       ! grep -qE '[A-Z]' <<<"$p1"; then
                        echo "    Tip: a strong password mixes upper/lower case, digits"
                        echo "         and symbols. You can keep the weak one if you want."
                    fi
                    printf "    Repeat: "
                    read -rs p2; echo
                    if [[ "$p1" != "$p2" ]]; then
                        echo "    Passwords do not match - try again."
                        continue
                    fi
                    AP_PASS="$p1"
                    return
                done
                ;;
            3)
                echo "    WARNING: the setup AP will be OPEN. Anyone nearby can join it"
                echo "            and see the config page / sniff the network."
                echo "            The device auto-powers-off after 15 minutes if no"
                echo "            network is configured by then."
                printf "    Type YES to confirm an open AP: "
                read -r confirm; echo
                if [[ "$confirm" == "YES" ]]; then
                    AP_PASS=""
                    return
                fi
                echo "    Cancelled - choose again."
                ;;
            *) echo "    Invalid choice." ;;
        esac
    done
}

upper() { printf '%s' "$1" | tr '[:lower:]' '[:upper:]'; }

detect_country() {
    # Country already configured on this system (wpa_supplicant conf / crda).
    local f c
    for f in /etc/wpa_supplicant/wpa_supplicant.conf \
             /etc/wpa_supplicant/wpa_supplicant-wlan0.conf; do
        c=$(sed -n 's/^[[:space:]]*country=["'\'']\{0,1\}\([A-Za-z][A-Za-z]\).*/\1/p' "$f" 2>/dev/null | head -n1 || true)
        [[ -n "$c" ]] && { upper "$c"; return; }
    done
    c=$(sed -n 's/^REGDOMAIN=["'\'']\{0,1\}\([A-Za-z][A-Za-z]\).*/\1/p' /etc/default/crda 2>/dev/null | head -n1 || true)
    [[ -n "$c" ]] && upper "$c"
    return 0   # "not found" is not an error (set -e + $(...) would abort)
}

ask_country() {
    # The WiFi regulatory country decides the legal channels / TX power.  Never
    # guess silently: use WIFI_COUNTRY, else what the system already has, else
    # ask.  Non-interactive without any of these: leave it unset (the daemon
    # then logs a warning and uses its fallback).
    COUNTRY="${WIFI_COUNTRY:-$(detect_country)}"
    if [[ -n "$COUNTRY" ]]; then
        echo "    WiFi country: $COUNTRY"
        return
    fi
    if [[ ! -t 0 ]]; then
        echo "    WARNING: no WiFi country configured - set WIFI_COUNTRY=<CC> in $ENV_FILE"
        return
    fi
    while true; do
        printf "    WiFi country (2-letter code, e.g. DE, GB, US): "
        read -r COUNTRY
        COUNTRY="$(upper "$COUNTRY")"
        [[ "$COUNTRY" =~ ^[A-Z]{2}$ ]] && return
        echo "    Please enter exactly two letters."
    done
}

env_quote() {
    # Quote a value for systemd's EnvironmentFile (double-quoted, escaped).
    printf '"%s"' "$(printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g')"
}

echo "==> wifi-setup installer"

# --- 0. Preflight -------------------------------------------------------------
if [[ $EUID -ne 0 ]]; then
    echo "    ERROR: run as root:  sudo bash $0" >&2
    exit 1
fi
# The daemon drives wlan0 through dhcpcd + wpa_supplicant directly (Raspberry
# Pi OS Bullseye).  Under NetworkManager (Bookworm and later by default) NM
# owns wlan0 and would fight the daemon over it — refuse before touching
# anything rather than leave a half-working network stack behind.
if systemctl is-active --quiet NetworkManager 2>/dev/null; then
    echo "    ERROR: NetworkManager is active on this system." >&2
    echo "           wifi-setup supports dhcpcd + wpa_supplicant networking only" >&2
    echo "           (Raspberry Pi OS Bullseye / Legacy). Nothing was changed." >&2
    exit 1
fi
if [[ -n "${WIFI_AP_PASSWORD:-}" ]]; then
    problem="$(ap_password_problem "$WIFI_AP_PASSWORD")"
    if [[ -n "$problem" ]]; then
        echo "    ERROR: WIFI_AP_PASSWORD is not a valid WPA2 password: $problem" >&2
        echo "           Nothing was changed." >&2
        exit 1
    fi
fi
if ! command -v dhcpcd >/dev/null 2>&1; then
    echo "    WARNING: dhcpcd not found - wifi-setup expects dhcpcd to manage"
    echo "             wlan0 in client mode (Raspberry Pi OS Bullseye)."
fi

# --- 1. Install packages ------------------------------------------------------
# Offline-safe: check what's missing first, and if installation fails (e.g. no
# internet) still continue copying files — the user gets a clear message and an
# exact command to finish the package install later.
PKGS="hostapd dnsmasq iw wireless-tools openssl"
missing=""
for p in $PKGS; do
    dpkg -s "$p" >/dev/null 2>&1 || missing="$missing $p"
done
if [[ -n "$missing" ]]; then
    echo "==> Installing missing packages:$missing  (needs internet)"
    if apt-get update >/dev/null 2>&1 \
       && DEBIAN_FRONTEND=noninteractive apt-get install -y $missing; then
        echo "    packages installed."
    else
        echo ""
        echo "    *** WARNING: could not install:$missing"
        echo "    ***   (no internet? run this later once online:)"
        echo "    ***   sudo apt-get update && sudo apt-get install -y$missing"
        echo "    *** The installer continues, but the setup AP needs these packages."
        echo ""
    fi
else
    echo "    all required packages already installed."
fi

# Keep hostapd/dnsmasq from auto-starting; the daemon starts them on demand.
systemctl disable hostapd || true
systemctl disable dnsmasq || true

# The per-interface wpa_supplicant unit is the daemon's preferred attach path
# (see ensure_wpa_supplicant_running): it binds wlan0 and is PONG-verified.
# Enabling it gives fresh devices the known-good path instead of relying on
# the direct -B fallback.  Tolerant: on an image without the @-unit the
# daemon simply falls back (the @wlan0 unit ships with the wpa_supplicant
# package, so this is essentially always present on Bullseye).
echo "==> Enabling wpa_supplicant@wlan0 (preferred per-interface attach path)"
if systemctl enable wpa_supplicant@wlan0 >/dev/null 2>&1; then
    echo "    enabled - the daemon will attach wlan0 via the per-interface unit"
    # One owner per interface.  With BOTH units enabled they fight over wlan0
    # during boot, before the daemon gets a chance to kill the loser: the
    # monolithic unit runs `-u -s -O /run/wpa_supplicant` with no `-i`, so it
    # never binds wlan0 itself (Changelog 0.6) but it does claim the control
    # directory and the scan schedule.  Observed as ~90s of "Reject scan
    # trigger since one is already pending" / "Failed to initiate AP scan" /
    # "Failed to initiate sched scan" while the daemon was trying to associate.
    # Only disabled now that we KNOW the @-unit exists, so an image without it
    # keeps whatever wpa_supplicant path it shipped with.  The daemon still
    # keeps the monolithic unit as its last-resort attach candidate.
    if systemctl disable wpa_supplicant >/dev/null 2>&1; then
        echo "    disabled monolithic wpa_supplicant.service - @wlan0 is the single owner of wlan0"
    else
        echo "    NOTE: could not disable wpa_supplicant.service; the daemon kills the"
        echo "          extra instance on attach, so this is a boot-noise issue only."
    fi
else
    echo "    NOTE: wpa_supplicant@wlan0 not available (unit missing on this image);"
    echo "          the daemon falls back to a direct wpa_supplicant -B."
fi

# --- 1b. Ensure SSH as the recovery escape hatch ------------------------------
# sshd runs independent of wlan0 mode, so it stays reachable on the setup AP at
# 192.168.4.1.  This is how to fix things if the web UI isn't enough.
echo "==> Ensuring SSH is enabled (recovery access on the setup AP)"
systemctl enable ssh 2>/dev/null || true
systemctl start ssh 2>/dev/null || true
if systemctl is-enabled ssh >/dev/null 2>&1; then
    echo "    ssh enabled: connect with 'ssh pi@192.168.4.1' while on the setup AP"
else
    echo "    NOTE: 'ssh' unit not found/not enabled (headless Pi? enable it manually)."
fi

# --- 2. Copy application files ------------------------------------------------
echo "==> Copying files to $DEST_DIR"
mkdir -p "$DEST_DIR"
# Replace the UI asset dirs instead of merging into them: `cp -r` on top of an
# existing dir keeps files that a newer version deleted, so an old index.html or
# app.js would survive a re-install and the browser would happily serve it.
rm -rf "$DEST_DIR/templates" "$DEST_DIR/static"
cp -r "$SRC_DIR/wifi_setup_daemon.py" \
      "$SRC_DIR/wifi_config_server.py" \
      "$SRC_DIR/wifi_scan.py" \
      "$SRC_DIR/templates" \
      "$SRC_DIR/static" \
      "$DEST_DIR/"
chmod +x "$DEST_DIR/wifi_setup_daemon.py" "$DEST_DIR/wifi_config_server.py"
chown -R root:root "$DEST_DIR"

# --- 3. Known-networks store --------------------------------------------------
mkdir -p "$CFG_DIR"
if [[ ! -f "$STORE_FILE" ]]; then
    # Prefer the repo's canonical seed in config/known-wifi.json.
    if [[ -f "$SRC_DIR/config/known-wifi.json" ]]; then
        echo "==> Pre-seeding $STORE_FILE from $SRC_DIR/config/known-wifi.json"
        cp "$SRC_DIR/config/known-wifi.json" "$STORE_FILE"
    elif [[ -f "$SRC_DIR/known-wifi.json" ]]; then
        echo "==> Pre-seeding $STORE_FILE from $SRC_DIR/known-wifi.json"
        cp "$SRC_DIR/known-wifi.json" "$STORE_FILE"
    else
        echo "==> Creating empty $STORE_FILE"
        printf '{\n  "networks": []\n}\n' > "$STORE_FILE"
    fi
fi
chmod 600 "$STORE_FILE"

# --- 4. Daemon env file (AP security / ports) ---------------------------------
# Preserves an existing file so re-runs don't clobber a configured AP password.
# On first install the user is asked how to secure the setup AP (unless
# WIFI_AP_PASSWORD was provided).
echo "==> Writing daemon env file to $ENV_FILE (if not present)"
if [[ -f "$ENV_FILE" ]]; then
    echo "    (keeping existing $ENV_FILE)"
else
    AP_PASS="${WIFI_AP_PASSWORD:-}"
    if [[ -z "$AP_PASS" ]]; then
        ask_ap_security
    fi
    ask_country
    {
        [[ -z "${AP_PASS:-}" ]]      || echo "WIFI_AP_PASSWORD=$(env_quote "$AP_PASS")"
        [[ -z "${WIFI_DEVICE_NAME:-}" ]] || echo "WIFI_DEVICE_NAME=$(env_quote "$WIFI_DEVICE_NAME")"
        [[ -z "${WIFI_AP_SSID:-}" ]] || echo "WIFI_AP_SSID=$(env_quote "$WIFI_AP_SSID")"
        [[ -z "${WIFI_AP_CHANNEL:-}" ]] || echo "WIFI_AP_CHANNEL=$(env_quote "$WIFI_AP_CHANNEL")"
        [[ -z "${WIFI_CONFIG_PORT:-}" ]] || echo "WIFI_CONFIG_PORT=$(env_quote "$WIFI_CONFIG_PORT")"
        [[ -z "${COUNTRY:-}" ]] || echo "WIFI_COUNTRY=$(env_quote "$COUNTRY")"
    } > "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    if [[ -n "${AP_PASS:-}" ]]; then
        echo "    setup AP will be password-protected (WPA2)."
    else
        echo "    WARNING: setup AP is OPEN (insecure) - the device will"
        echo "             auto-power-off after 15 min if nothing is configured."
    fi
fi

# --- 5. systemd unit ----------------------------------------------------------
echo "==> Installing systemd unit"
cp "$SRC_DIR/systemd/$SERVICE" "/etc/systemd/system/$SERVICE"
systemctl daemon-reload
systemctl enable "$SERVICE"

echo ""
echo "==> Done."
echo "    Service      : $SERVICE (enabled on boot)"
echo "    Networks     : $STORE_FILE"
echo "    Daemon env   : $ENV_FILE"
echo ""
echo "    If the Pi has internet, run:  systemctl start $SERVICE"
echo "    Known networks are NOT forgotten on re-runs."
echo "    Recovery while in setup mode:  ssh pi@192.168.4.1  (and http(s)://192.168.4.1)"
if [[ ! -f "$ENV_FILE" ]] || ! grep -q WIFI_AP_PASSWORD "$ENV_FILE"; then
    echo ""
    echo "    SECURITY: the setup AP is currently OPEN. Re-secure it by editing"
    echo "      $ENV_FILE  (add WIFI_AP_PASSWORD=...)  then: systemctl restart $SERVICE"
fi
