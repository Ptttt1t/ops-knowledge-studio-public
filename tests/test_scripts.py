from __future__ import annotations

import os
import unittest

from scripts.start_server import _background_process_options


class ScriptTests(unittest.TestCase):
    def test_background_server_options_are_platform_specific(self):
        options = _background_process_options()
        if os.name == "nt":
            self.assertIn("creationflags", options)
            self.assertNotIn("start_new_session", options)
        else:
            self.assertEqual(options, {"start_new_session": True})


if __name__ == "__main__":
    unittest.main()
