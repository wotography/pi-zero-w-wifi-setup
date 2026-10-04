import support  # noqa: F401  (must come first: isolates env)

import os
import unittest
from unittest import mock

import wifi_scan


class NamingTests(unittest.TestCase):

    def test_device_name_from_env(self):
        with mock.patch.dict(os.environ, {"WIFI_DEVICE_NAME": "Kitchen"}):
            self.assertEqual(wifi_scan.device_name(), "Kitchen")

    def test_device_name_falls_back_to_hostname(self):
        with mock.patch.dict(os.environ, {"WIFI_DEVICE_NAME": ""}), \
                mock.patch("socket.gethostname", return_value="pi-zero.lan"):
            self.assertEqual(wifi_scan.device_name(), "pi-zero")

    def test_ap_ssid_default_and_override(self):
        self.assertEqual(wifi_scan.default_ap_ssid("Kitchen"), "Kitchen-Setup")
        with mock.patch.dict(os.environ, {"WIFI_AP_SSID": "MyAP"}):
            self.assertEqual(wifi_scan.ap_ssid(), "MyAP")

    def test_ap_ssid_fits_32_bytes(self):
        for name in ("x" * 63, "a-very-long-hostname-that-goes-on", "Küche " * 10):
            ssid = wifi_scan.default_ap_ssid(name)
            self.assertLessEqual(len(ssid.encode("utf-8")), 32, ssid)
            self.assertTrue(ssid.endswith("-Setup"))
            self.assertNotIn("--Setup", ssid)

    def test_setup_domain_is_dns_safe(self):
        self.assertEqual(wifi_scan.setup_domain("Living Room"), "living-room.setup")
        self.assertEqual(wifi_scan.setup_domain("Küche_2"), "k-che-2.setup")
        self.assertEqual(wifi_scan.setup_domain("---"), "device.setup")
        with mock.patch.dict(os.environ, {"WIFI_SETUP_DOMAIN": "Setup.Example"}):
            self.assertEqual(wifi_scan.setup_domain(), "setup.example")


if __name__ == "__main__":
    unittest.main()
