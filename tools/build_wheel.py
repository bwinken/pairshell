#!/usr/bin/env python3
"""Build pairshell's wheel with the standard library only.

`pip install` of a source tree needs setuptools from PyPI (build isolation),
which fails behind TLS-intercepting proxies and on airgapped machines.  A
pure-Python wheel needs neither: `pip install dist/pairshell-<v>-py3-none-any.whl`.

    python tools/build_wheel.py            -> dist/pairshell-<version>-py3-none-any.whl
"""

from __future__ import annotations

import base64
import hashlib
import sys
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _record_hash(data: bytes) -> str:
    return "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()


def build(dist_dir: Path | None = None) -> Path:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    name, version = project["name"], project["version"]
    dist_dir = dist_dir or ROOT / "dist"
    dist_dir.mkdir(parents=True, exist_ok=True)
    wheel = dist_dir / f"{name}-{version}-py3-none-any.whl"
    info = f"{name}-{version}.dist-info"

    files: list[tuple[str, bytes]] = []
    pkg = ROOT / "pairshell"
    for path in sorted(pkg.rglob("*")):
        if path.is_dir() or "__pycache__" in path.parts or path.suffix in (".pyc", ".pyo"):
            continue
        files.append((path.relative_to(ROOT).as_posix(), path.read_bytes()))

    classifiers = "".join(f"Classifier: {c}\n" for c in project.get("classifiers", []))
    urls = "".join(f"Project-URL: {k}, {v}\n" for k, v in project.get("urls", {}).items())
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    metadata = (
        "Metadata-Version: 2.1\n"
        f"Name: {name}\n"
        f"Version: {version}\n"
        f"Summary: {project.get('description', '')}\n"
        f"Author: {', '.join(a.get('name', '') for a in project.get('authors', []))}\n"
        f"License: {project.get('license', {}).get('text', 'MIT')}\n"
        f"Keywords: {','.join(project.get('keywords', []))}\n"
        + urls
        + classifiers
        + f"Requires-Python: {project.get('requires-python', '>=3.11')}\n"
        "Description-Content-Type: text/markdown\n"
        "License-File: LICENSE\n"
        "\n" + readme
    )
    files.append((f"{info}/METADATA", metadata.encode("utf-8")))
    files.append((f"{info}/WHEEL", b"Wheel-Version: 1.0\nGenerator: pairshell-tools\nRoot-Is-Purelib: true\nTag: py3-none-any\n"))
    scripts = project.get("scripts", {})
    ep = "[console_scripts]\n" + "".join(f"{k} = {v}\n" for k, v in scripts.items())
    files.append((f"{info}/entry_points.txt", ep.encode("utf-8")))
    files.append((f"{info}/top_level.txt", b"pairshell\n"))
    files.append((f"{info}/LICENSE", (ROOT / "LICENSE").read_bytes()))

    record_lines = [f"{arc},{_record_hash(data)},{len(data)}" for arc, data in files]
    record_lines.append(f"{info}/RECORD,,")
    with zipfile.ZipFile(wheel, "w", zipfile.ZIP_DEFLATED) as zf:
        for arc, data in files:
            zf.writestr(zipfile.ZipInfo(arc, date_time=(2026, 1, 1, 0, 0, 0)), data)
        zf.writestr(zipfile.ZipInfo(f"{info}/RECORD", date_time=(2026, 1, 1, 0, 0, 0)), "\n".join(record_lines) + "\n")
    return wheel


if __name__ == "__main__":
    out = build(Path(sys.argv[1]) if len(sys.argv) > 1 else None)
    print(out)
