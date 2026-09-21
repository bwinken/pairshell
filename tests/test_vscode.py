"""The bundled VS Code extension: package, vsix, settings merge, stub run."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from pairshell import __version__, vsix

ROOT = Path(__file__).resolve().parent.parent
NODE = shutil.which("node")


class BundleTests(unittest.TestCase):
    def test_bundle_matches_source_and_version(self):
        bundled = json.loads((vsix.EXT_DIR / "package.json").read_text(encoding="utf-8"))
        source = json.loads((ROOT / "vscode" / "package.json").read_text(encoding="utf-8"))
        self.assertEqual(bundled, source, "run tools/build_vscode_bundle.py")
        self.assertEqual(bundled["version"], __version__)
        js = (vsix.EXT_DIR / "out" / "extension.js").read_text(encoding="utf-8")
        for marker in ("pairshell.attach", "list", "--json", "pairshell.openLog", "pairshell.switchCurrent"):
            self.assertIn(marker, js)

    def test_build_vsix(self):
        with tempfile.TemporaryDirectory() as d:
            path = vsix.build_vsix(Path(d) / vsix.default_vsix_name())
            with zipfile.ZipFile(path) as zf:
                names = set(zf.namelist())
                self.assertTrue({"extension.vsixmanifest", "[Content_Types].xml", "extension/package.json", "extension/out/extension.js"} <= names)
                manifest = ET.fromstring(zf.read("extension.vsixmanifest"))
                ns = {"v": "http://schemas.microsoft.com/developer/vsx-schema/2011"}
                ident = manifest.find("v:Metadata/v:Identity", ns)
                self.assertEqual(ident.get("Id"), "pairshell-vscode")
                self.assertEqual(ident.get("Version"), __version__)
                self.assertEqual(ident.get("Publisher"), "pairshell")
                pkg = json.loads(zf.read("extension/package.json"))
                self.assertEqual(pkg["main"], "./out/extension.js")


class SettingsMergeTests(unittest.TestCase):
    KEY = "terminal.integrated.profiles.windows"
    VAL = {"path": "pairshell"}

    def test_empty_file(self):
        text, changes = vsix.merge_settings_text("", self.KEY, self.VAL)
        data = json.loads(text)
        self.assertEqual(data[self.KEY]["pairshell"], self.VAL)
        self.assertEqual(data["terminal.integrated.defaultLocation"], "editor")
        self.assertEqual(len(changes), 2)

    def test_strict_json_keeps_other_keys_and_is_idempotent(self):
        original = json.dumps({"editor.fontSize": 14, self.KEY: {"PowerShell": {"source": "PowerShell"}}, "terminal.integrated.defaultLocation": "view"}, indent=2)
        text, changes = vsix.merge_settings_text(original, self.KEY, self.VAL, extension_path="python -m pairshell")
        data = json.loads(text)
        self.assertEqual(data["editor.fontSize"], 14)
        self.assertEqual(data[self.KEY]["PowerShell"], {"source": "PowerShell"})
        self.assertEqual(data[self.KEY]["pairshell"], self.VAL)
        self.assertEqual(data["terminal.integrated.defaultLocation"], "view")  # user's choice kept
        self.assertEqual(data["pairshell.path"], "python -m pairshell")
        self.assertEqual(sorted(changes), sorted([f"{self.KEY}.pairshell", "pairshell.path = python -m pairshell"]))
        again, changes2 = vsix.merge_settings_text(text, self.KEY, self.VAL, extension_path="python -m pairshell")
        self.assertEqual((again, changes2), (text, []))

    def test_jsonc_keeps_comments(self):
        original = """{
    // my editor
    "editor.fontSize": 14, /* trailing */
    "terminal.integrated.profiles.windows": {
        "Git Bash": { "source": "Git Bash" },
    },
}
"""
        text, changes = vsix.merge_settings_text(original, self.KEY, self.VAL)
        self.assertIn("// my editor", text)
        self.assertIn('"pairshell": {"path": "pairshell"},', text)
        self.assertIn('"Git Bash"', text)
        self.assertIn('"terminal.integrated.defaultLocation": "editor",', text)
        self.assertEqual(len(changes), 2)
        # the result is still valid JSONC: strip comments/trailing commas and parse
        data = json.loads(vsix._strip_jsonc(text))
        self.assertEqual(data[self.KEY]["pairshell"], self.VAL)
        self.assertEqual(data[self.KEY]["Git Bash"], {"source": "Git Bash"})
        again, changes2 = vsix.merge_settings_text(text, self.KEY, self.VAL)
        self.assertEqual((again, changes2), (text, []))

    def test_jsonc_without_profiles_key(self):
        original = "{\n  // nothing yet\n  \"window.zoomLevel\": 1\n}\n"
        text, changes = vsix.merge_settings_text(original, self.KEY, self.VAL, default_location=False)
        data = json.loads(vsix._strip_jsonc(text))
        self.assertEqual(data[self.KEY]["pairshell"], self.VAL)
        self.assertEqual(data["window.zoomLevel"], 1)
        self.assertNotIn("terminal.integrated.defaultLocation", data)
        self.assertEqual(changes, [f"{self.KEY}.pairshell"])


class InstallVscodeCliTests(unittest.TestCase):
    def test_vsix_only_and_settings_path(self):
        with tempfile.TemporaryDirectory() as d:
            settings = Path(d) / "User" / "settings.json"
            out = Path(d) / "ext.vsix"
            r = subprocess.run(
                [sys.executable, "-m", "pairshell", "install-vscode", "--vsix-only", str(out), "--settings-path", str(settings)],
                capture_output=True, text=True, cwd=str(ROOT),
            )
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertTrue(out.exists() and zipfile.is_zipfile(out))
            data = json.loads(settings.read_text(encoding="utf-8"))
            self.assertIn("pairshell", data[vsix.platform_profiles_key()])
            self.assertIn("updated", r.stderr)
            r = subprocess.run(
                [sys.executable, "-m", "pairshell", "install-vscode", "--no-extension", "--settings-path", str(settings)],
                capture_output=True, text=True, cwd=str(ROOT),
            )
            self.assertIn("already had", r.stderr)


@unittest.skipUnless(NODE, "needs node")
class StubRunTests(unittest.TestCase):
    def test_extension_runs_under_stub_vscode(self):
        # With tmux around the harness also runs a real exec that outlives its
        # timeout and checks the pending command shows up in the UI.
        with_exec = sys.platform != "win32" and bool(shutil.which("tmux")) and bool(shutil.which("bash"))
        session = f"vsstub{os.getpid()}"
        with tempfile.TemporaryDirectory() as d:
            env = dict(os.environ, PAIRSHELL_HOME=d, HOME=d)
            subprocess.run([sys.executable, "-m", "pairshell", "add", "dev", "--protocol", "local", "--session", session], capture_output=True, env=env, cwd=str(ROOT), check=True)
            try:
                r = subprocess.run(
                    [NODE, str(ROOT / "tests" / "vscode_stub_harness.js"), str(vsix.EXT_DIR / "out" / "extension.js"), f"{sys.executable} -m pairshell", *(["--with-exec"] if with_exec else [])],
                    capture_output=True, text=True, env=env, cwd=str(ROOT), timeout=180,
                )
            finally:
                if with_exec:
                    subprocess.run([sys.executable, "-m", "pairshell", "stop", "dev"], capture_output=True, env=env, cwd=str(ROOT))
                    subprocess.run(["tmux", "kill-session", "-t", f"={session}:"], capture_output=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("[harness] OK", r.stdout)
            if with_exec:
                self.assertIn("[harness] pending command shown", r.stdout)


if __name__ == "__main__":
    unittest.main()
