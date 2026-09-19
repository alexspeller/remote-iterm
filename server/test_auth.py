import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

from server.auth import (
    COOKIE_NAME,
    access_key_path,
    is_trusted_origin,
    is_valid_key,
    key_from_cookie_header,
    load_or_create_key,
)


class AccessKeyTest(unittest.TestCase):
    def test_creates_and_reuses_a_private_machine_key(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config" / "access-key"

            first = load_or_create_key(path)
            second = load_or_create_key(path)

            self.assertEqual(first, second)
            self.assertGreaterEqual(len(first), 40)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)

    def test_environment_can_override_the_key_location(self):
        with mock.patch.dict(os.environ, {"REMOTE_ITERM_KEY_FILE": "~/custom-key"}):
            self.assertEqual(access_key_path(), Path.home() / "custom-key")

    def test_validates_only_an_exact_string_key(self):
        self.assertTrue(is_valid_key("secret", "secret"))
        self.assertFalse(is_valid_key("secret", "wrong"))
        self.assertFalse(is_valid_key("secret", None))
        self.assertFalse(is_valid_key(None, "secret"))


class KeyCookieTest(unittest.TestCase):
    def test_reads_the_key_from_the_auth_cookie(self):
        header = f"other=1; {COOKIE_NAME}=the-key; another=2"
        self.assertEqual(key_from_cookie_header(header), "the-key")

    def test_ignores_missing_empty_or_malformed_cookies(self):
        self.assertIsNone(key_from_cookie_header(None))
        self.assertIsNone(key_from_cookie_header(""))
        self.assertIsNone(key_from_cookie_header("other=1"))
        self.assertIsNone(key_from_cookie_header(f"{COOKIE_NAME}="))
        self.assertIsNone(key_from_cookie_header("not a cookie header"))


class TrustedOriginTest(unittest.TestCase):
    """Credentialed CORS is allowed only for pages served by this machine: the
    client lives on another port of the same host, so the origin's host must
    match the Host header the API was reached at."""

    def test_same_host_on_another_port_is_trusted(self):
        self.assertTrue(is_trusted_origin("http://100.83.49.69:7292", "100.83.49.69:7291"))
        self.assertTrue(is_trusted_origin("http://localhost:7292", "localhost:7291"))
        self.assertTrue(is_trusted_origin("http://Mac.local:7292", "mac.local:7291"))
        self.assertTrue(is_trusted_origin("http://[::1]:7292", "[::1]:7291"))

    def test_other_hosts_are_not_trusted(self):
        self.assertFalse(is_trusted_origin("http://evil.example:7292", "100.83.49.69:7291"))
        self.assertFalse(is_trusted_origin("http://100.83.49.69.evil.example", "100.83.49.69:7291"))
        self.assertFalse(is_trusted_origin("http://localhost:7292", "127.0.0.1:7291"))

    def test_rejects_missing_or_malformed_values(self):
        self.assertFalse(is_trusted_origin(None, "100.83.49.69:7291"))
        self.assertFalse(is_trusted_origin("null", "100.83.49.69:7291"))
        self.assertFalse(is_trusted_origin("http://100.83.49.69:7292", None))
        self.assertFalse(is_trusted_origin("file:///index.html", "100.83.49.69:7291"))
        self.assertFalse(is_trusted_origin("100.83.49.69:7292", "100.83.49.69:7291"))


if __name__ == "__main__":
    unittest.main()
