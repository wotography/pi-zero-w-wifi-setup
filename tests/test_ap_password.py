"""Setup-AP password rules: printable ASCII, 8-63 characters (IEEE 802.11i).

A "€" in the AP password made the setup AP impossible to join from some
devices, and the installer used to generate such passwords itself.
"""
import support  # noqa: F401  (must come first: isolates env)

import os
import re
import shutil
import subprocess
import unittest

import wifi_setup_daemon as D

INSTALLER = os.path.join(support.ROOT, "wifi_setup.sh")

GOOD = ["Abc12345", "with space ok!", "Ab#$%&()*+,-./:;<>?@[]_9", "x" * 63]
BAD = ["Ab€12345", "Glück1234", "short", "x" * 64]


def installer_functions(*names):
    """Source only the named shell functions (and AP_* vars) from the installer."""
    with open(INSTALLER, encoding="utf-8") as f:
        text = f.read()
    parts = [l for l in text.splitlines() if re.match(r"^AP_(CHARS|GEN_LEN)=", l)]
    for name in names:
        m = re.search(r"^%s\(\) \{\n.*?^\}\n" % name, text, re.S | re.M)
        parts.append(m.group(0))
    return "\n".join(parts)


class DaemonCheckTests(unittest.TestCase):

    def test_good(self):
        for pw in GOOD:
            self.assertIsNone(D.ap_password_problem(pw), pw)

    def test_bad(self):
        for pw in BAD:
            self.assertIsNotNone(D.ap_password_problem(pw), pw)


@unittest.skipUnless(shutil.which("bash"), "bash not available")
class InstallerTests(unittest.TestCase):

    def bash(self, script, *args):
        return subprocess.run(["bash", "-c", script, "_"] + list(args),
                              capture_output=True, text=True, check=True).stdout

    def test_charset_is_printable_ascii(self):
        line = next(l for l in open(INSTALLER, encoding="utf-8")
                    if l.startswith("AP_CHARS="))
        chars = line.split("=", 1)[1].strip().strip("'")
        self.assertTrue(chars)
        self.assertTrue(all(33 <= ord(c) <= 126 for c in chars), chars)

    def test_installer_check_matches_daemon(self):
        fn = installer_functions("ap_password_problem")
        for pw in GOOD + BAD:
            out = self.bash(fn + '\nap_password_problem "$1"', pw).strip()
            self.assertEqual(bool(out), D.ap_password_problem(pw) is not None, pw)

    def test_generated_passwords_are_valid(self):
        fn = installer_functions("random_ap_password")
        out = self.bash(fn + '\nfor i in 1 2 3 4 5 6 7 8 9 10; do random_ap_password; echo; done')
        passwords = out.split("\n")[:10]
        for pw in passwords:
            self.assertEqual(len(pw), 16, repr(pw))
            self.assertIsNone(D.ap_password_problem(pw), pw)


if __name__ == "__main__":
    unittest.main()
