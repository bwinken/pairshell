"""The zero-install launcher and the CLI's --help."""

import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class LauncherTests(unittest.TestCase):
    def test_bin_launcher_runs_from_any_cwd(self):
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        r = subprocess.run([sys.executable, str(ROOT / "bin" / "pairshell"), "--version"], capture_output=True, text=True, cwd="/", env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(r.stdout.startswith("pairshell "), r.stdout)

    def test_help_lists_commands_and_examples(self):
        r = subprocess.run([sys.executable, "-m", "pairshell", "--help"], capture_output=True, text=True, cwd=str(ROOT))
        self.assertEqual(r.returncode, 0)
        for word in ("attach", "exec", "screen", "keys", "status", "examples:", "exit codes"):
            self.assertIn(word, r.stdout)
        r = subprocess.run([sys.executable, "-m", "pairshell", "exec", "--help"], capture_output=True, text=True, cwd=str(ROOT))
        self.assertIn("--timeout", r.stdout)

    def test_cmd_launcher_has_crlf_and_no_bom(self):
        data = (ROOT / "bin" / "pairshell.cmd").read_bytes()
        self.assertTrue(data.startswith(b"@echo off\r\n"))
        self.assertNotIn(b"\n\n", data.replace(b"\r\n", b"\n") + b"x")


if __name__ == "__main__":
    unittest.main()
