import support  # noqa: F401  (must come first: isolates env)

import json
import os
import stat
import unittest

import wifi_config_server as S

# IEEE 802.11i test vector: wpa_passphrase IEEE password
IEEE_PSK = "f42c6fc52df0ebef9ebb4b90b38a5f902e83fe1b135a70e23aed762e9710a12e"


class StoreTests(unittest.TestCase):

    def setUp(self):
        self.store = os.path.join(support.TMP, "store-%s.json" % self.id())
        S.STORE_FILE = self.store

    def read(self):
        with open(self.store) as f:
            return json.load(f)["networks"]

    def test_psk_matches_wpa_passphrase(self):
        self.assertEqual(S.wpa2_psk("IEEE", "password"), IEEE_PSK)

    def test_save_stores_only_the_hash(self):
        S.save_network("IEEE", "password")
        self.assertEqual(self.read(), [{"ssid": "IEEE", "psk": IEEE_PSK}])

    def test_store_is_0600(self):
        S.save_network("Net", "password1")
        self.assertEqual(stat.S_IMODE(os.stat(self.store).st_mode), 0o600)

    def test_open_network(self):
        S.save_network("Cafe", "")
        self.assertEqual(self.read(), [{"ssid": "Cafe", "password": ""}])

    def test_empty_password_keeps_saved_credential(self):
        S.save_network("Home", "password1")
        S.save_network("Home", "")
        self.assertEqual(self.read()[0]["psk"], S.wpa2_psk("Home", "password1"))

    def test_preferred_is_tristate(self):
        S.save_network("Home", "password1", preferred=True)
        S.save_network("Home", "password2")                    # None: keep
        self.assertTrue(self.read()[0]["preferred"])
        S.save_network("Home", "password2", preferred=False)   # clear
        self.assertNotIn("preferred", self.read()[0])

    def test_single_preferred(self):
        S.save_network("A", "password1", preferred=True)
        S.save_network("B", "password2")
        S.set_preferred("B")
        flags = {n["ssid"]: n.get("preferred") for n in self.read()}
        self.assertEqual(flags, {"A": None, "B": True})
        self.assertIsNone(S.set_preferred("unknown"))

    def test_forget(self):
        S.save_network("A", "password1")
        S.save_network("B", "password2")
        S.delete_network("A")
        self.assertEqual([n["ssid"] for n in self.read()], ["B"])

    def test_public_view_redacts_secrets(self):
        S.save_network("A", "password1", preferred=True)
        self.assertEqual(S.public_wlans(), [{"ssid": "A", "preferred": True}])


if __name__ == "__main__":
    unittest.main()
