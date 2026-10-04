import support  # noqa: F401  (must come first: isolates env)

import json
import os
import unittest


class RepoHygieneTests(unittest.TestCase):

    def test_seed_store_ships_empty(self):
        # The installer copies config/known-wifi.json onto every new device:
        # a credential committed here would be published AND installed.
        with open(os.path.join(support.ROOT, "config", "known-wifi.json")) as f:
            self.assertEqual(json.load(f), {"networks": []})


if __name__ == "__main__":
    unittest.main()
