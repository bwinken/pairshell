"""Build and install the VS Code extension without node, and set up settings.json.

The compiled extension ships in ``pairshell/vscode_ext``; this module zips it
into a ``.vsix`` (the layout ``vsce`` produces), hands it to the ``code`` CLI,
and merges the terminal-profile settings into the user's ``settings.json``
while leaving comments and unrelated keys alone.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

EXT_DIR = Path(__file__).resolve().parent / "vscode_ext"


class VsixError(Exception):
    pass


# --------------------------------------------------------------------------
# .vsix
# --------------------------------------------------------------------------


def extension_manifest() -> dict[str, Any]:
    try:
        return json.loads((EXT_DIR / "package.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise VsixError("the compiled extension is not bundled in this copy of pairshell (run tools/build_vscode_bundle.py)") from None


def _vsixmanifest(pkg: dict[str, Any]) -> str:
    props = {
        "Microsoft.VisualStudio.Code.Engine": pkg.get("engines", {}).get("vscode", "^1.85.0"),
        "Microsoft.VisualStudio.Code.ExtensionDependencies": "",
        "Microsoft.VisualStudio.Code.ExtensionPack": "",
        "Microsoft.VisualStudio.Code.ExtensionKind": "workspace,ui",
        "Microsoft.VisualStudio.Code.LocalizedLanguages": "",
    }
    prop_xml = "\n".join(f'      <Property Id="{k}" Value="{escape(v, {chr(34): "&quot;"})}" />' for k, v in props.items())
    return f"""<?xml version="1.0" encoding="utf-8"?>
<PackageManifest Version="2.0.0" xmlns="http://schemas.microsoft.com/developer/vsx-schema/2011" xmlns:d="http://schemas.microsoft.com/developer/vsx-schema-design/2011">
  <Metadata>
    <Identity Language="en-US" Id="{escape(pkg["name"])}" Version="{escape(pkg["version"])}" Publisher="{escape(pkg["publisher"])}" />
    <DisplayName>{escape(pkg.get("displayName", pkg["name"]))}</DisplayName>
    <Description xml:space="preserve">{escape(pkg.get("description", ""))}</Description>
    <Tags>{escape(",".join(pkg.get("keywords", [])))}</Tags>
    <Categories>{escape(",".join(pkg.get("categories", ["Other"])))}</Categories>
    <GalleryFlags>Public</GalleryFlags>
    <Properties>
{prop_xml}
    </Properties>
    <License>extension/LICENSE.txt</License>
  </Metadata>
  <Installation>
    <InstallationTarget Id="Microsoft.VisualStudio.Code" />
  </Installation>
  <Dependencies />
  <Assets>
    <Asset Type="Microsoft.VisualStudio.Code.Manifest" Path="extension/package.json" Addressable="true" />
    <Asset Type="Microsoft.VisualStudio.Services.Content.Details" Path="extension/README.md" Addressable="true" />
    <Asset Type="Microsoft.VisualStudio.Services.Content.License" Path="extension/LICENSE.txt" Addressable="true" />
  </Assets>
</PackageManifest>
"""


CONTENT_TYPES = """<?xml version="1.0" encoding="utf-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension=".json" ContentType="application/json" />
  <Default Extension=".vsixmanifest" ContentType="text/xml" />
  <Default Extension=".js" ContentType="application/javascript" />
  <Default Extension=".md" ContentType="text/markdown" />
  <Default Extension=".svg" ContentType="image/svg+xml" />
  <Default Extension=".txt" ContentType="text/plain" />
</Types>
"""


def default_vsix_name() -> str:
    pkg = extension_manifest()
    return f"{pkg['name']}-{pkg['version']}.vsix"


def build_vsix(dest: Path) -> Path:
    """Zip the bundled extension into ``dest`` (a ``.vsix`` path)."""
    pkg = extension_manifest()
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    files = [
        ("extension/package.json", EXT_DIR / "package.json"),
        ("extension/out/extension.js", EXT_DIR / "out" / "extension.js"),
        ("extension/media/icon.svg", EXT_DIR / "media" / "icon.svg"),
        ("extension/README.md", EXT_DIR / "README.md"),
        ("extension/LICENSE.txt", EXT_DIR / "LICENSE.txt"),
    ]
    for _, src in files:
        if not src.exists():
            raise VsixError(f"bundled extension is incomplete: missing {src.name}")
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("extension.vsixmanifest", _vsixmanifest(pkg))
        zf.writestr("[Content_Types].xml", CONTENT_TYPES)
        for arcname, src in files:
            zf.write(src, arcname)
    return dest


# --------------------------------------------------------------------------
# code CLI
# --------------------------------------------------------------------------


def find_code_cli(insiders: bool = False) -> str | None:
    names = ["code-insiders"] if insiders else ["code"]
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    app = "Microsoft VS Code Insiders" if insiders else "Microsoft VS Code"
    exe = "code-insiders" if insiders else "code"
    candidates: list[Path] = []
    if sys.platform == "win32":
        for base in (os.environ.get("LOCALAPPDATA"), os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
            if base:
                candidates.append(Path(base) / "Programs" / app / "bin" / f"{exe}.cmd")
                candidates.append(Path(base) / app / "bin" / f"{exe}.cmd")
    elif sys.platform == "darwin":
        candidates.append(Path("/Applications") / f"Visual Studio Code{' - Insiders' if insiders else ''}.app" / "Contents" / "Resources" / "app" / "bin" / exe)
    else:
        candidates += [Path("/usr/bin") / exe, Path("/usr/local/bin") / exe, Path("/snap/bin") / exe]
    for c in candidates:
        if c.exists():
            return str(c)
    return None


def install_extension(vsix: Path, insiders: bool = False) -> tuple[bool, str]:
    cli = find_code_cli(insiders)
    if not cli:
        return False, "VS Code's `code` command was not found (Command Palette: 'Shell Command: Install code command in PATH')"
    try:
        r = subprocess.run([cli, "--install-extension", str(vsix), "--force"], capture_output=True, text=True, timeout=180, shell=sys.platform == "win32" and cli.lower().endswith(".cmd"))
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"could not run {cli}: {exc}"
    out = (r.stdout + r.stderr).strip()
    return r.returncode == 0, out or f"exit code {r.returncode}"


# --------------------------------------------------------------------------
# settings.json
# --------------------------------------------------------------------------


def user_settings_path(insiders: bool = False) -> Path:
    folder = "Code - Insiders" if insiders else "Code"
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / folder / "User" / "settings.json"


def platform_profiles_key() -> str:
    return {"win32": "terminal.integrated.profiles.windows", "darwin": "terminal.integrated.profiles.osx"}.get(sys.platform, "terminal.integrated.profiles.linux")


def pairshell_command() -> tuple[list[str], str]:
    """How VS Code should start pairshell: ``(argv, one-line form)``.

    Prefers the ``pairshell`` command on PATH, then a zero-install launcher
    next to this package, then ``<python> -m pairshell``.
    """
    if shutil.which("pairshell"):
        return ["pairshell"], "pairshell"
    root = Path(__file__).resolve().parent.parent
    launcher = root / "bin" / ("pairshell.cmd" if sys.platform == "win32" else "pairshell")
    if launcher.exists() and not any(part in ("site-packages", "dist-packages") for part in root.parts):
        return [str(launcher)], str(launcher)
    return [sys.executable, "-m", "pairshell"], f"{sys.executable} -m pairshell"


def _strip_jsonc(text: str) -> str:
    """Remove comments and trailing commas so JSONC can be checked for emptiness."""
    out: list[str] = []
    i, n = 0, len(text)
    in_str = False
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
            i += 1
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
        else:
            out.append(ch)
            i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def _indent_of(text: str) -> str:
    m = re.search(r"\n([ \t]+)\"", text)
    return m.group(1) if m else "    "


def merge_settings_text(
    text: str,
    profiles_key: str,
    profile_value: dict[str, Any],
    extension_path: str | None = None,
    default_location: bool = True,
) -> tuple[str, list[str]]:
    """Return the new settings text and a list of changes made (empty = nothing to do).

    Strict JSON is loaded, merged and re-dumped.  JSONC (comments, trailing
    commas) is edited textually so the user's comments survive: new keys go
    to the top of the root object, the ``pairshell`` profile to the top of
    an existing profiles object.
    """
    changes: list[str] = []
    stripped = _strip_jsonc(text).strip()
    if not stripped or stripped == "{}":
        data: dict[str, Any] = {profiles_key: {"pairshell": profile_value}}
        changes.append(f"{profiles_key}.pairshell")
        if default_location:
            data["terminal.integrated.defaultLocation"] = "editor"
            changes.append("terminal.integrated.defaultLocation = editor")
        if extension_path:
            data["pairshell.path"] = extension_path
            changes.append(f"pairshell.path = {extension_path}")
        return json.dumps(data, indent=4, ensure_ascii=False) + "\n", changes

    try:
        data = json.loads(text)
        strict = isinstance(data, dict)
    except ValueError:
        strict = False

    if strict:
        profiles = data.setdefault(profiles_key, {})
        if not isinstance(profiles, dict):
            profiles = data[profiles_key] = {}
        if profiles.get("pairshell") != profile_value:
            profiles["pairshell"] = profile_value
            changes.append(f"{profiles_key}.pairshell")
        if default_location and "terminal.integrated.defaultLocation" not in data:
            data["terminal.integrated.defaultLocation"] = "editor"
            changes.append("terminal.integrated.defaultLocation = editor")
        if extension_path and data.get("pairshell.path") != extension_path:
            data["pairshell.path"] = extension_path
            changes.append(f"pairshell.path = {extension_path}")
        if not changes:
            return text, []
        indent = len(_indent_of(text).expandtabs(4)) or 4
        return json.dumps(data, indent=indent, ensure_ascii=False) + "\n", changes

    # JSONC: textual edits
    indent = _indent_of(text)
    new_text = text
    entry = json.dumps(profile_value, ensure_ascii=False)
    m = re.search(r'"' + re.escape(profiles_key) + r'"\s*:\s*\{', new_text)
    if m:
        body_start = m.end()
        # is there already a pairshell entry inside this object?
        depth, j = 1, body_start
        while j < len(new_text) and depth:
            if new_text[j] == "{":
                depth += 1
            elif new_text[j] == "}":
                depth -= 1
            j += 1
        if re.search(r'"pairshell"\s*:', new_text[body_start:j]) is None:
            new_text = new_text[:body_start] + f'\n{indent}{indent}"pairshell": {entry},' + new_text[body_start:]
            changes.append(f"{profiles_key}.pairshell")
    else:
        root = new_text.find("{")
        if root < 0:
            raise VsixError("settings.json does not look like a JSON object")
        new_text = new_text[: root + 1] + f'\n{indent}"{profiles_key}": {{ "pairshell": {entry} }},' + new_text[root + 1 :]
        changes.append(f"{profiles_key}.pairshell")
    if default_location and re.search(r'"terminal\.integrated\.defaultLocation"\s*:', new_text) is None:
        root = new_text.find("{")
        new_text = new_text[: root + 1] + f'\n{indent}"terminal.integrated.defaultLocation": "editor",' + new_text[root + 1 :]
        changes.append("terminal.integrated.defaultLocation = editor")
    if extension_path and re.search(r'"pairshell\.path"\s*:', new_text) is None:
        root = new_text.find("{")
        new_text = new_text[: root + 1] + f'\n{indent}"pairshell.path": {json.dumps(extension_path, ensure_ascii=False)},' + new_text[root + 1 :]
        changes.append(f"pairshell.path = {extension_path}")
    return new_text, changes


def apply_settings(path: Path, default_location: bool = True) -> list[str]:
    """Merge pairshell's settings into ``path`` (backup first).  Returns the changes."""
    argv, one_line = pairshell_command()
    profile_value: dict[str, Any] = {"path": argv[0]}
    if len(argv) > 1:
        profile_value["args"] = argv[1:]
    extension_path = None if one_line == "pairshell" else one_line
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    new_text, changes = merge_settings_text(text, platform_profiles_key(), profile_value, extension_path, default_location)
    if changes:
        path.parent.mkdir(parents=True, exist_ok=True)
        if text:
            shutil.copy(path, path.with_name(path.name + ".pairshell.bak"))
        path.write_text(new_text, encoding="utf-8")
    return changes


__all__ = [
    "VsixError",
    "apply_settings",
    "build_vsix",
    "default_vsix_name",
    "extension_manifest",
    "find_code_cli",
    "install_extension",
    "merge_settings_text",
    "pairshell_command",
    "platform_profiles_key",
    "user_settings_path",
]
