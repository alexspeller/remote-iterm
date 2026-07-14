import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

from server.auth import access_key_path, is_valid_key, load_or_create_key


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


if __name__ == "__main__":
    unittest.main()
