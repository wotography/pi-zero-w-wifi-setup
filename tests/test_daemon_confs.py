import support  # noqa: F401  (must come first: isolates env)

import os
import time
import unittest
from unittest import mock

import wifi_setup_daemon as D


class WpaConfTests(unittest.TestCase):

    def setUp(self):
        self.path = os.path.join(support.TMP, "wpa-%s.conf" % self.id())

    def write(self, nets):
        D._write_wpa_conf(self.path, nets)
        with open(self.path) as f:
            return f.read()

    def test_priorities_and_credentials(self):
        conf = self.write([
            {"ssid": "Home", "psk": "a" * 64, "preferred": True},
            {"ssid": "Cafe", "password": ""},
            {"ssid": 'Quote"d', "password": "plain-pass"},
        ])
        self.assertIn("update_config=0", conf)
        self.assertIn("country=GB", conf)
        home, cafe, quoted = conf.split("network={")[1:]
        self.assertIn("priority=%d" % D.PREFERRED_PRIORITY, home)
        self.assertIn("psk=" + "a" * 64, home)
        self.assertIn("priority=%d" % D.DEFAULT_PRIORITY, cafe)
        self.assertIn("key_mgmt=NONE", cafe)
        self.assertIn('ssid="Quote\\"d"', quoted)
        self.assertNotIn("disabled=1", conf)

    def test_rewrite_does_not_duplicate(self):
        self.write([{"ssid": "A", "password": ""}])
        conf = self.write([{"ssid": "B", "password": ""}])
        self.assertEqual(conf.count("network={"), 1)
        self.assertEqual(conf.count("# " + D._WPA_MANAGED_MARKER), 1)
        self.assertNotIn('ssid="A"', conf)

    def test_foreign_conf_keeps_head_drops_networks(self):
        with open(self.path, "w") as f:
            f.write("ctrl_interface=DIR=/run/x\nupdate_config=1\ncountry=FR\n"
                    "network={\n    ssid=\"Old\"\n}\n")
        conf = self.write([{"ssid": "New", "password": ""}])
        self.assertIn("country=FR", conf)
        self.assertIn("update_config=0", conf)
        self.assertNotIn("update_config=1", conf)
        self.assertNotIn('ssid="Old"', conf)


class DnsmasqTests(unittest.TestCase):

    def test_captive_wildcard_and_domain(self):
        text = D.dnsmasq_conf_text()
        self.assertIn("address=/%s/%s" % (D.SETUP_DOMAIN, D.AP_IP), text)
        self.assertIn("address=/#/" + D.AP_IP, text)
        self.assertIn("bind-dynamic", text)
        self.assertNotIn("bind-interfaces", text)

    def test_captive_off(self):
        with mock.patch.object(D, "CAPTIVE_PORTAL", False):
            self.assertNotIn("address=/#/", D.dnsmasq_conf_text())


class TimeTests(unittest.TestCase):

    def test_uptime_field(self):
        rec = mock.Mock()
        self.assertTrue(D._UptimeFilter().filter(rec))
        h, m, s = rec.uptime.split(":")
        self.assertTrue(h.isdigit() and len(m) == 2 and len(s) == 2)

    def test_clock_step_logged_once(self):
        D._clock_offset = time.time() - time.monotonic() - 3700
        with mock.patch.object(D, "long_log") as long_log:
            D._check_clock_step()
            D._check_clock_step()
        self.assertEqual(long_log.call_count, 1)
        self.assertIn("+3700s", long_log.call_args[0][0])


if __name__ == "__main__":
    unittest.main()
