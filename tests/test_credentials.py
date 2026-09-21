"""Credential store: Windows round trip (skipped elsewhere) and fallbacks."""

import os
import sys
import unittest

from pairshell import credentials


class CredentialTests(unittest.TestCase):
    def test_backend_name(self):
        self.assertIn(credentials.backend(), ("windows-credential-manager", "macos-keychain", "secret-tool", "none"))

    def test_target_name(self):
        self.assertEqual(credentials.target_name("lab1"), "pairshell:lab1")

    def test_env_override(self):
        old = os.environ.get(credentials.ENV_PASSWORD)
        os.environ[credentials.ENV_PASSWORD] = "hunter2"
        try:
            self.assertEqual(credentials.resolve_password("whatever"), "hunter2")
        finally:
            if old is None:
                os.environ.pop(credentials.ENV_PASSWORD, None)
            else:
                os.environ[credentials.ENV_PASSWORD] = old

    @unittest.skipUnless(sys.platform == "win32", "Windows Credential Manager only")
    def test_windows_round_trip(self):
        name = "__pairshell_test__"
        credentials.delete_password(name)
        self.assertIsNone(credentials.load_password(name))
        credentials.store_password(name, "alice", "p@ss wörd ☃")
        try:
            self.assertEqual(credentials.load_password(name), "p@ss wörd ☃")
            self.assertTrue(credentials.has_password(name))
        finally:
            credentials.delete_password(name)
        self.assertIsNone(credentials.load_password(name))

    @unittest.skipIf(sys.platform == "win32" or credentials.backend() != "none", "fallback path only")
    def test_no_backend_behaviour(self):
        self.assertIsNone(credentials.load_password("nothing"))
        with self.assertRaises(credentials.CredentialError):
            credentials.store_password("nothing", "u", "p")
        credentials.delete_password("nothing")  # no error


if __name__ == "__main__":
    unittest.main()
