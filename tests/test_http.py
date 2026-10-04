"""Config-server HTTP behaviour, driven in-process over a socketpair.

No port is bound and every system command is stubbed, so this runs on any
machine (and in sandboxes that forbid listening sockets).
"""
import support  # noqa: F401  (must come first: isolates env)

import json
import re
import socket
import unittest
from unittest import mock

import wifi_config_server as S

BASE = "127.0.0.1:8901"


def request(raw):
    a, b = socket.socketpair()
    try:
        a.sendall(raw.encode())
        a.shutdown(socket.SHUT_WR)
        S.Handler(b, ("127.0.0.1", 5555), None)
        b.close()
        out = b"".join(iter(lambda: a.recv(65536), b""))
    finally:
        a.close()
    head, _, body = out.partition(b"\r\n\r\n")
    lines = head.decode().split("\r\n")
    headers = dict(l.split(": ", 1) for l in lines[1:] if ": " in l)
    return int(lines[0].split()[1]), headers, body.decode("utf-8", "replace")


def get(path, host=BASE):
    return request("GET %s HTTP/1.1\r\nHost: %s\r\n\r\n" % (path, host))


def post(path, body="{}", host=BASE, origin=None):
    extra = "Origin: %s\r\n" % origin if origin else ""
    return request("POST %s HTTP/1.1\r\nHost: %s\r\n%sContent-Type: application/json\r\n"
                   "Content-Length: %d\r\n\r\n%s" % (path, host, extra, len(body), body))


class HttpTests(unittest.TestCase):

    def setUp(self):
        self.calls = []
        run = mock.patch.object(S, "run", side_effect=lambda cmd, timeout=30:
                                (self.calls.append(cmd), (1, ""))[1])
        timer = mock.patch.object(S.threading, "Timer",
                                  side_effect=lambda d, f, a=(): mock.Mock(start=lambda: f(*a)))
        run.start()
        timer.start()
        self.addCleanup(run.stop)
        self.addCleanup(timer.stop)
        S.STORE_FILE = support.os.path.join(support.TMP, "http-store.json")

    def test_captive_probe_redirects(self):
        for host, path in (("captive.apple.com", "/hotspot-detect.html"),
                           ("connectivitycheck.gstatic.com", "/generate_204")):
            status, headers, body = get(path, host)
            self.assertEqual(status, 302)
            self.assertEqual(headers["Location"], "http://127.0.0.1:8901/")
            self.assertEqual(headers["Cache-Control"], "no-store")
            self.assertEqual(body, "")

    def test_foreign_host_never_gets_api_data(self):
        status, _, body = get("/api/status", "evil.example")
        self.assertEqual(status, 302)
        self.assertNotIn("networks", body)

    def test_captive_off_forbids(self):
        with mock.patch.object(S, "CAPTIVE_PORTAL", False):
            self.assertEqual(get("/", "captive.apple.com")[0], 403)

    def test_post_guards(self):
        self.assertEqual(post("/api/connect", '{"ssid":"x"}', host="evil.example")[0], 403)
        self.assertEqual(post("/api/shutdown", origin="http://evil.example")[0], 403)
        self.assertEqual(self.calls, [])

    def test_setup_domain_is_allowed(self):
        self.assertEqual(get("/", S.SETUP_DOMAIN)[0], 200)

    def test_page_is_rendered_with_device_name(self):
        status, _, page = get("/")
        self.assertEqual(status, 200)
        self.assertEqual(re.search(r"<title>(.*?)</title>", page).group(1),
                         "Test-Device WiFi Setup")
        self.assertIn('data-ap-ssid="Test-Device-Setup"', page)
        self.assertIn(">Shutdown Test-Device<", page)
        self.assertNotIn("{{", page)
        self.assertRegex(page, r'<input id="preferred" type="checkbox">')

    def test_page_escapes_name(self):
        with mock.patch.object(S, "DEVICE_NAME", 'A"<x-evil>'):
            page = get("/")[2]
        self.assertIn("A&quot;&lt;x-evil&gt; WiFi Setup", page)
        self.assertNotIn("<x-evil>", page)

    def test_status(self):
        data = json.loads(get("/api/status")[2])
        self.assertEqual(data["device_name"], "Test-Device")
        self.assertEqual(data["ap_ssid"], "Test-Device-Setup")

    def test_shutdown_answers_then_powers_off(self):
        status, _, body = post("/api/shutdown")
        self.assertEqual(status, 200)
        self.assertIn("Shutting down Test-Device", json.loads(body)["message"])
        self.assertEqual(self.calls[-1], ["systemctl", "poweroff"])

    def test_connect_validates_password(self):
        status, _, body = post("/api/connect", '{"ssid":"Net","password":"short"}')
        self.assertEqual(status, 400)
        self.assertFalse(json.loads(body)["ok"])


if __name__ == "__main__":
    unittest.main()
