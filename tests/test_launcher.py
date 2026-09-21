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


class SkillTests(unittest.TestCase):
    def test_bundled_skill_has_frontmatter(self):
        text = (ROOT / "pairshell" / "skill" / "SKILL.md").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\nname: pairshell\ndescription: "))
        for word in ("rc", "124", "--force", "screen", "keys", "tcsh"):
            self.assertIn(word, text)

    def test_install_skill_user_and_project(self):
        import tempfile

        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as proj:
            env = dict(os.environ, HOME=home, USERPROFILE=home)
            r = subprocess.run([sys.executable, "-m", "pairshell", "install-skill", "--user"], capture_output=True, text=True, cwd=str(ROOT), env=env)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertTrue((Path(home) / ".claude" / "skills" / "pairshell" / "SKILL.md").exists())
            r = subprocess.run([sys.executable, "-m", "pairshell", "install-skill"], capture_output=True, text=True, cwd=proj, env=dict(env, PYTHONPATH=str(ROOT)))
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertTrue((Path(proj) / ".claude" / "skills" / "pairshell" / "SKILL.md").exists())
            r = subprocess.run([sys.executable, "-m", "pairshell", "install-skill", "--print"], capture_output=True, text=True, cwd=str(ROOT), env=env)
            self.assertTrue(r.stdout.startswith("---\nname: pairshell"))


class DetachKeyTests(unittest.TestCase):
    def test_parse_detach_key(self):
        from pairshell.attach import AttachError, parse_detach_key

        self.assertEqual(parse_detach_key(None), (b"\x1d", "Ctrl-]"))
        self.assertEqual(parse_detach_key("C-]"), (b"\x1d", "Ctrl-]"))
        self.assertEqual(parse_detach_key("^]"), (b"\x1d", "Ctrl-]"))
        self.assertEqual(parse_detach_key("ctrl-q"), (b"\x11", "Ctrl-Q"))
        self.assertEqual(parse_detach_key("C-\\"), (b"\x1c", "Ctrl-\\"))
        for bad in ("F12", "q", "C-", "alt-x", "C-]]"):
            with self.assertRaises(AttachError):
                parse_detach_key(bad)


class RunDirTests(unittest.TestCase):
    def test_run_dir_locations(self):
        import importlib
        import tempfile

        from pairshell import profiles

        saved = {k: os.environ.get(k) for k in ("PAIRSHELL_HOME", "XDG_STATE_HOME", "XDG_CONFIG_HOME")}
        try:
            with tempfile.TemporaryDirectory() as d:
                os.environ["PAIRSHELL_HOME"] = d
                self.assertEqual(profiles.run_dir(), Path(d) / "run")
                os.environ.pop("PAIRSHELL_HOME")
                os.environ["XDG_STATE_HOME"] = d
                if sys.platform != "win32":
                    self.assertEqual(profiles.run_dir(), Path(d) / "pairshell" / "run")
                    self.assertNotEqual(profiles.run_dir().parent, profiles.config_dir())
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


class WheelTests(unittest.TestCase):
    def test_build_wheel_offline_install(self):
        import shutil
        import tempfile
        import zipfile

        sys.path.insert(0, str(ROOT / "tools"))
        try:
            import build_wheel  # noqa: E402
        finally:
            sys.path.pop(0)
        from pairshell import __version__

        with tempfile.TemporaryDirectory() as d:
            whl = build_wheel.build(Path(d))
            self.assertEqual(whl.name, f"pairshell-{__version__}-py3-none-any.whl")
            with zipfile.ZipFile(whl) as zf:
                names = zf.namelist()
                info = f"pairshell-{__version__}.dist-info"
                for required in (f"{info}/METADATA", f"{info}/WHEEL", f"{info}/RECORD", f"{info}/entry_points.txt",
                                 "pairshell/cli.py", "pairshell/skill/SKILL.md", "pairshell/vscode_ext/out/extension.js"):
                    self.assertIn(required, names)
                self.assertFalse(any("__pycache__" in n for n in names))
                self.assertIn("pairshell = pairshell.cli:main", zf.read(f"{info}/entry_points.txt").decode())
                record = zf.read(f"{info}/RECORD").decode().splitlines()
                self.assertEqual(len(record), len(names))
            if shutil.which(sys.executable) and subprocess.run([sys.executable, "-m", "pip", "--version"], capture_output=True).returncode == 0:
                target = Path(d) / "site"
                r = subprocess.run([sys.executable, "-m", "pip", "install", "--no-index", "--no-deps", "--quiet", "--target", str(target), str(whl)], capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, r.stderr)
                env = dict(os.environ, PYTHONPATH=str(target))
                r = subprocess.run([sys.executable, "-m", "pairshell", "--version"], capture_output=True, text=True, cwd="/", env=env)
                self.assertEqual(r.stdout.strip(), f"pairshell {__version__}")
