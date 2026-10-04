const listEl = document.getElementById("networks");
// "Reload last scan results" (GET /api/scan, no AP drop).  Not on the page at
// the moment — the list loads by itself and after a Rescan — but kept wired:
// adding <button id="reload-scan-btn"> to index.html brings it back.
const reloadScanBtn = document.getElementById("reload-scan-btn");
const rescanBtn = document.getElementById("rescan-btn");
const scanMsgEl = document.getElementById("scan-msg");
const connForm = document.getElementById("connect-form");
const listSection = document.getElementById("network-list");
const backBtn = document.getElementById("back-btn");
const chosenEl = document.getElementById("chosen-ssid");
const saveForm = document.getElementById("save-form");
const passwordEl = document.getElementById("password");
const msgEl = document.getElementById("msg");
const logSection = document.getElementById("log-section");
const logEl = document.getElementById("log");
const warnBanner = document.getElementById("unsafe-banner");
const countdownEl = document.getElementById("countdown");
const manualSsidEl = document.getElementById("manual-ssid");
const manualBtn = document.getElementById("manual-btn");
const backMsgEl = document.getElementById("back-msg");
const exitSetupBtn = document.getElementById("exit-setup-btn");
const savedListEl = document.getElementById("saved-networks");
const savedMsgEl = document.getElementById("saved-msg");
const preferredEl = document.getElementById("preferred");
const menuBtn = document.getElementById("menu-btn");
const menuEl = document.getElementById("menu");
const shutdownBtn = document.getElementById("shutdown-btn");
const confirmBox = document.getElementById("confirm-box");
const confirmText = document.getElementById("confirm-text");
const confirmOk = document.getElementById("confirm-ok");
const confirmCancel = document.getElementById("confirm-cancel");

let chosenSsid = null;
let pendingMode = "live";
let countdownTimer = null;
let knownSsids = [];
let savedNets = [];
let preferredSsids = new Set();
let storeSig = null;
// Rendered into <body data-…> by the config server; /api/status keeps them
// current.  Only a fallback label is needed when the page is opened raw.
let deviceName = document.body.dataset.deviceName || "";
let apSsid = document.body.dataset.apSsid || "";

if (reloadScanBtn) reloadScanBtn.addEventListener("click", reloadLastScan);
backBtn.addEventListener("click", showList);
manualBtn.addEventListener("click", useManual);
manualSsidEl.addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); useManual(); }
});
saveForm.addEventListener("submit", onSubmit);
saveForm.querySelectorAll("button[data-mode]").forEach(b => {
  b.addEventListener("click", () => { pendingMode = b.dataset.mode; });
});
exitSetupBtn.addEventListener("click", () => { closeMenu(); exitSetupMode(); });
shutdownBtn.addEventListener("click", () => { closeMenu(); shutdownDevice(); });
menuBtn.addEventListener("click", (e) => {
  e.stopPropagation();
  if (menuEl.classList.contains("hidden")) openMenu(); else closeMenu();
});
document.addEventListener("click", (e) => {
  if (!menuEl.contains(e.target)) closeMenu();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") { closeMenu(); closeConfirm(false); }
});
rescanBtn.addEventListener("click", doRescan);
refreshStatus().then(reloadLastScan);

// Loads the network list the daemon recorded in client mode just before the
// setup AP started.  It does NOT scan: the radio cannot scan while it is the
// AP — that is what Rescan (doRescan) is for.
async function reloadLastScan() {
  if (reloadScanBtn) {
    reloadScanBtn.disabled = true;
    reloadScanBtn.textContent = "Loading...";
  }
  try {
    const res = await fetch("/api/scan");
    const data = await res.json();
    renderNetworks(data.networks || [], data.cached);
  } catch (e) {
    // With no reload button on the page, offer the retry right here.
    listEl.innerHTML = '<li class="muted">Could not load the network list (' +
      esc(String(e)) + '). <a href="#" id="retry-scan">Try again</a></li>';
    const retry = document.getElementById("retry-scan");
    retry.addEventListener("click", (ev) => { ev.preventDefault(); reloadLastScan(); });
  } finally {
    if (reloadScanBtn) {
      reloadScanBtn.disabled = false;
      reloadScanBtn.textContent = "Reload last scan results";
    }
  }
}

function renderNetworks(nets, cached) {
  listEl.innerHTML = "";
  const withSsid = (nets || []).filter(n => n.ssid);
  if (!withSsid.length) {
    scanMsgEl.textContent = "";
    listEl.innerHTML = '<li class="muted">No networks found.</li>' +
      '<li class="muted">Tap <b>Rescan</b> (the setup AP restarts for a moment to ' +
      'scan), or type the network name below.</li>';
    return;
  }
  const inRange = withSsid.filter(n => knownSsids.includes(n.ssid));
  if (inRange.length) {
    exitSetupBtn.textContent = "Leave setup mode & reconnect to " + inRange[0].ssid;
  }
  scanMsgEl.textContent = cached
    ? "Networks detected while the device was connecting."
    : "";
  for (const n of withSsid) {
    const li = document.createElement("li");
    const signal = n.signal == null ? 0 : Math.max(0, Math.min(100, (n.signal + 100) * 1.3));
    // The badges sit NEXT TO the name, not inside it: the name truncates with
    // an ellipsis on a narrow phone, and the marks must never be cut off.
    const isSaved = knownSsids.includes(n.ssid);
    const saved = isSaved
      ? '<span class="saved" title="Saved network">&#10003;</span>'
      : "";
    const fav = isSaved && preferredSsids.has(n.ssid)
      ? '<span class="star-badge" title="Home network - tried first">&#9733;</span>'
      : "";
    const sec = secLabel(n.security);
    li.innerHTML =
      '<button class="net" data-ssid="' + esc(n.ssid) + '">' +
      '<span class="net-name"><span class="ssid">' + esc(n.ssid) + '</span>' + saved + fav + '</span>' +
      '<span class="sig">' + sigBars(signal) + (sec ? " <b>" + esc(sec) + "</b>" : " <i>open</i>") + '</span>' +
      '</button>';
    li.querySelector("button").addEventListener("click", () => pick(n.ssid));
    listEl.appendChild(li);
  }
}

async function doRescan() {
  rescanBtn.disabled = true;
  scanMsgEl.textContent = "Restarting the setup AP to scan. This device will reconnect in a few seconds...";
  try {
    await fetch("/api/rescan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
  } catch (e) {
    // The AP drops mid-request while wlan0 scans — expected.
  }
  // The setup AP is down for a few seconds; poll until it answers again.
  let tries = 0;
  const retry = async () => {
    tries++;
    try {
      const res = await fetch("/api/scan");
      const data = await res.json();
      renderNetworks(data.networks || [], data.cached);
      scanMsgEl.textContent = "Rescan complete.";
      rescanBtn.disabled = false;
    } catch (e) {
      if (tries < 40) {
        setTimeout(retry, 1500);
      } else {
        scanMsgEl.textContent = "Could not reach the setup AP again. Rejoin '" + (apSsid || "the setup WiFi") + "' and reload this page.";
        rescanBtn.disabled = false;
      }
    }
  };
  setTimeout(retry, 4000);
}

function useManual() {
  const ssid = manualSsidEl.value.trim();
  if (!ssid) { manualSsidEl.focus(); return; }
  pick(ssid);
}

function pick(ssid) {
  chosenSsid = ssid;
  chosenEl.textContent = ssid;
  passwordEl.value = "";
  msgEl.textContent = "";
  // Default: unticked — the star is an explicit choice.  The one exception is
  // the network that already IS the home network: the form always sends
  // `preferred`, so an unticked box there would silently clear the star on a
  // password correction or a reconnect.
  preferredEl.checked = preferredSsids.has(ssid);
  listSection.classList.add("hidden");
  connForm.classList.remove("hidden");
}

function showList() {
  connForm.classList.add("hidden");
  listSection.classList.remove("hidden");
  chosenSsid = null;
}

async function onSubmit(e) {
  e.preventDefault();
  msgEl.textContent = "Saving...";
  showLog(false);
  try {
    const res = await fetch("/api/connect", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        ssid: chosenSsid,
        password: passwordEl.value,
        mode: pendingMode,
        preferred: preferredEl.checked,
      }),
    });
    const data = await res.json();
    const btns = saveForm.querySelectorAll("button");
    if (!data.ok) {
      msgEl.textContent = data.message || ("Error: " + (data.error || "unknown"));
      loadLog();
      return;
    }
    // Answer arrives BEFORE the setup AP goes down (live) or the device
    // reboots, so the message + log tail below are guaranteed to reach the user.
    msgEl.textContent = data.message ||
      "Password accepted. Saved. Reconnecting...";
    if (data.log_tail) {
      renderLog(data.log_tail);
    }
    btns.forEach(b => b.disabled = true);
    showLog(true);
  } catch (err) {
    // The AP was already gone when we answered — that is the designed switch.
    // Never show a raw network error for a deliberate disconnect.
    msgEl.textContent = "The device is switching networks — the setup AP has " +
      "gone down. If it does not reappear, rejoin " + esc(chosenSsid || "your WiFi") +
      " or wait for the setup AP to return on its own.";
    showLog(true);
  }
}

function renderLog(lines) {
  const arr = Array.isArray(lines) ? lines : [];
  logEl.textContent = arr.join("\n") || "(no log output yet)";
}

async function loadLog() {
  try {
    const res = await fetch("/api/log");
    const data = await res.json();
    renderLog(data.log || []);
    showLog(true);
  } catch (e) {
    logEl.textContent = "Could not load log: " + String(e);
  }
}

function showLog(show) {
  logSection.classList.toggle("hidden", !show);
}

async function exitSetupMode() {
  exitSetupBtn.disabled = true;
  backMsgEl.textContent = "Requesting to leave setup mode...";
  try {
    const res = await fetch("/api/normal", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    const data = await res.json();
    if (data.ok) {
      backMsgEl.textContent = data.message + " Disconnecting from the setup AP shortly; join your home WiFi.";
      exitSetupBtn.textContent = "Leaving setup mode";
    } else {
      backMsgEl.textContent = data.message || ("Error: " + (data.error || "unknown"));
      backMsgEl.textContent += " (keep at least one saved network for this option)";
      exitSetupBtn.disabled = false;
      exitSetupBtn.textContent = "Leave setup mode & reconnect";
    }
  } catch (err) {
    backMsgEl.textContent = "Setup AP going down. Reconnect your device to your home WiFi.";
    exitSetupBtn.textContent = "Leaving setup mode";
  }
}

function openMenu() {
  menuEl.classList.remove("hidden");
  menuBtn.setAttribute("aria-expanded", "true");
}

function closeMenu() {
  menuEl.classList.add("hidden");
  menuBtn.setAttribute("aria-expanded", "false");
}

function deviceLabel() {
  return deviceName || "the device";
}

// window.confirm() replacement.  The captive-portal sheet on macOS/iOS never
// shows JS dialogs — confirm() just returns false there, which made Shutdown
// and Forget silently do nothing.  Resolves true (OK) or false (Cancel/Esc).
let confirmResolve = null;

function askConfirm(text, okLabel) {
  closeConfirm(false);
  confirmText.textContent = text;
  confirmOk.textContent = okLabel || "OK";
  confirmBox.classList.remove("hidden");
  confirmCancel.focus();
  return new Promise(resolve => { confirmResolve = resolve; });
}

function closeConfirm(result) {
  if (!confirmResolve) return;
  const resolve = confirmResolve;
  confirmResolve = null;
  confirmBox.classList.add("hidden");
  resolve(result);
}

confirmOk.addEventListener("click", () => closeConfirm(true));
confirmCancel.addEventListener("click", () => closeConfirm(false));
confirmBox.addEventListener("click", (e) => {
  if (e.target === confirmBox) closeConfirm(false);   // tap on the backdrop
});

async function shutdownDevice() {
  // Field devices have no power button and no remote way back on: make the
  // consequence explicit before the one tap that cannot be undone over WiFi.
  const ok = await askConfirm(
    "Shut down " + deviceLabel() + "?\n\n" +
    "The device powers off completely. It only starts again when its power " +
    "is unplugged and plugged back in \u2014 there is no way to switch it on " +
    "remotely.", "Shut down");
  if (!ok) return;
  shutdownBtn.disabled = true;
  exitSetupBtn.disabled = true;
  backMsgEl.textContent = "Requesting shutdown...";
  try {
    const data = await postJson("/api/shutdown", {});
    if (!data.ok) {
      backMsgEl.textContent = "Error: " + (data.error || "unknown");
      shutdownBtn.disabled = false;
      exitSetupBtn.disabled = false;
      return;
    }
    backMsgEl.textContent = data.message || "Shutting down.";
  } catch (err) {
    // The setup AP vanished before the answer arrived — the designed outcome.
    backMsgEl.textContent = "Shutting down " + deviceLabel() + ". The setup AP is going away.";
  }
}

// Compact security label so the row keeps room for the name and its badges
// (the wpa_cli scan fallback reports e.g. "[WPA2-PSK-CCMP][WPS]").
function secLabel(s) {
  const t = String(s || "").toUpperCase();
  if (!t) return "";
  if (t.includes("SAE") || t.includes("WPA3")) return "WPA3";
  if (t.includes("RSN") || t.includes("WPA2")) return "WPA2";
  if (t.includes("WPA")) return "WPA";
  if (t.includes("WEP")) return "WEP";
  return t.replace(/[\[\]]/g, " ").trim().split(/\s+/)[0];
}

function sigBars(v) {
  const filled = Math.max(1, Math.round(v / 25));
  return "&#9610;".repeat(filled) + "&#9617;".repeat(4 - filled);
}

function esc(s) {
  const d = document.createElement("div");
  d.textContent = s;
  return d.innerHTML;
}

function renderSaved() {
  if (!savedListEl) return;
  savedListEl.innerHTML = "";
  if (!savedNets.length) {
    savedListEl.innerHTML = '<li class="muted">No networks saved yet.</li>';
    return;
  }
  for (const n of savedNets) {
    const fav = !!n.preferred;
    const li = document.createElement("li");
    li.innerHTML =
      '<span class="name">' + esc(n.ssid) + "</span>" +
      '<button class="mini star ' + (fav ? "star-on" : "") + '" type="button"' +
      (fav ? ' aria-pressed="true"' : ' aria-pressed="false"') +
      ' title="' + (fav ? "Currently the home network - tap to clear" : "Make this the home network") + '">' +
      (fav ? "&#9733; home" : "&#9734; home") + "</button>" +
      '<button class="mini danger" type="button" title="Forget this network">Forget</button>';
    const [starBtn, delBtn] = li.querySelectorAll("button");
    starBtn.addEventListener("click", () => setPreferred(n.ssid, !fav, starBtn, delBtn));
    delBtn.addEventListener("click", () => forgetNetwork(n.ssid, starBtn, delBtn));
    savedListEl.appendChild(li);
  }
}

async function postJson(path, body) {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return res.json();
}

async function setPreferred(ssid, wanted, ...buttons) {
  buttons.forEach(b => (b.disabled = true));
  savedMsgEl.textContent = wanted ? "Saving..." : "Removing home network...";
  try {
    const data = await postJson("/api/preferred", { ssid: ssid, preferred: wanted });
    if (!data.ok) {
      savedMsgEl.textContent = "Error: " + (data.error || "unknown");
      return;
    }
    applyStore(data.networks);
    savedMsgEl.textContent = (data.message || "Saved.") + " " + (data.note || "");
  } catch (e) {
    savedMsgEl.textContent = "Could not save: " + esc(String(e));
  } finally {
    buttons.forEach(b => (b.disabled = false));
  }
}

async function forgetNetwork(ssid, ...buttons) {
  // One tap must not be enough to drop a credential that is the device's only
  // way back onto a network.
  const ok = await askConfirm(
    "Forget the saved network \"" + ssid + "\"?\n\n" +
    "The device will no longer try to connect to it. Saved networks " +
    "cannot be re-added without its password again.", "Forget");
  if (!ok) return;
  buttons.forEach(b => (b.disabled = true));
  savedMsgEl.textContent = "Forgetting...";
  try {
    const data = await postJson("/api/forget", { ssid: ssid });
    if (!data.ok) {
      savedMsgEl.textContent = "Error: " + (data.error || "unknown");
      return;
    }
    applyStore(data.networks);
    savedMsgEl.textContent = data.message || "Forgotten.";
  } catch (e) {
    savedMsgEl.textContent = "Could not forget: " + esc(String(e));
  } finally {
    buttons.forEach(b => (b.disabled = false));
  }
}

function applyStore(nets) {
  savedNets = (nets || []).filter(n => n && n.ssid);
  knownSsids = savedNets.map(n => n.ssid);
  preferredSsids = new Set(savedNets.filter(n => n.preferred).map(n => n.ssid));
  // refreshStatus() is re-run every second while the open-AP countdown runs, so
  // only touch the DOM when the store actually changed — otherwise the list
  // would flicker and every second would re-fetch /api/scan for nothing.
  const sig = JSON.stringify(savedNets);
  if (sig === storeSig) return;
  storeSig = sig;
  renderSaved();
  if (listEl && listEl.children.length) reloadLastScan();   // refresh the star badges
}

async function refreshStatus() {
  try {
    const res = await fetch("/api/status");
    const data = await res.json();
    applyStore(data.networks);
    if (data.ap_ssid) apSsid = data.ap_ssid;
    if (data.device_name && data.device_name !== deviceName) {
      deviceName = data.device_name;
      shutdownBtn.textContent = "Shutdown " + deviceName;
    }
    const s = data.seconds_left;
    if (s != null && s > 0) {
      warnBanner.classList.remove("hidden");
      countdownEl.textContent = fmtTime(s);
      if (!countdownTimer) countdownTimer = setInterval(refreshStatus, 1000);
    } else if (s === 0) {
      warnBanner.classList.remove("hidden");
      countdownEl.textContent = "now \u2014 shutting down";
      clearInterval(countdownTimer);
      countdownTimer = null;
    } else {
      warnBanner.classList.add("hidden");
      clearInterval(countdownTimer);
      countdownTimer = null;
    }
  } catch (_) {
    // Setup AP gone (rebooting)? Just leave the banner as it is.
  }
}

function fmtTime(s) {
  const m = String(Math.floor(s / 60)).padStart(2, "0");
  const sec = String(s % 60).padStart(2, "0");
  return m + ":" + sec;
}
