"""Static consistency checks between templates/index.html and static/app.js."""
import support  # noqa: F401  (must come first: isolates env)

import os
import re
import unittest

# Elements app.js looks up but deliberately tolerates being absent (it checks
# for null before using them).  Anything else missing from the page would make
# app.js throw at load time and leave the whole setup page dead.
OPTIONAL_IDS = {
    "reload-scan-btn",   # "Reload last scan results" — wired, not on the page
    "retry-scan",        # created at runtime inside the error message
}


def read(*parts):
    with open(os.path.join(support.ROOT, *parts), encoding="utf-8") as f:
        return f.read()


class UiConsistencyTests(unittest.TestCase):

    def test_every_required_element_exists(self):
        js = read("static", "app.js")
        page = read("templates", "index.html")
        used = set(re.findall(r'getElementById\("([^"]+)"\)', js))
        present = set(re.findall(r'\bid="([^"]+)"', page))
        missing = used - present - OPTIONAL_IDS
        self.assertFalse(missing, "app.js needs ids missing from index.html: %s"
                         % sorted(missing))

    def test_optional_reload_button_is_guarded(self):
        js = read("static", "app.js")
        self.assertIn("if (reloadScanBtn)", js)


if __name__ == "__main__":
    unittest.main()
