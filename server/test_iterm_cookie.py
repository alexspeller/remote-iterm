import os
import unittest
from unittest.mock import patch

import iterm2.auth

from server.iterm_cookie import use_osascript_for_cookie


class OsascriptCookieTest(unittest.TestCase):
    def setUp(self):
        original = iterm2.auth.gAppKitAvailable
        self.addCleanup(setattr, iterm2.auth, "gAppKitAvailable", original)
        # authenticate() keeps a cookie already in the environment and stores
        # the one it fetches there, so give each test its own.
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("ITERM2_COOKIE", None)
        os.environ.pop("ITERM2_KEY", None)

    def test_cookie_is_requested_through_an_osascript_child(self):
        # The precondition that makes the fix necessary: pyobjc is installed,
        # so left alone the library would run the AppleScript in-process.
        self.assertTrue(iterm2.auth.gAppKitAvailable)

        use_osascript_for_cookie()

        runners = []

        def request_cookie_and_key(launch_if_needed, myname, runner_class):
            runners.append(runner_class)
            return "the-cookie the-key"

        with patch.object(iterm2.auth, "request_cookie_and_key",
                          request_cookie_and_key), \
                patch.object(iterm2.auth, "applescript_auth_disabled",
                             return_value=False):
            self.assertTrue(iterm2.auth.authenticate())

        self.assertEqual(runners, [iterm2.auth.CommandLineApplescriptRunner])
        self.assertEqual(os.environ["ITERM2_COOKIE"], "the-cookie")
        self.assertEqual(os.environ["ITERM2_KEY"], "the-key")


if __name__ == "__main__":
    unittest.main()
