#!/usr/bin/env python3
"""Compile vscode/src/extension.ts and copy the runtime files into
pairshell/vscode_ext/, which ships inside the Python package so that
`pairshell install-vscode` can build a .vsix without node on the workstation.

Uses vscode/node_modules/.bin/tsc when `npm install` was run (full type
check), otherwise a globally installed tsc without type resolution.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "vscode"
DEST = ROOT / "pairshell" / "vscode_ext"


def compile_ts() -> Path:
    out = SRC / "out"
    shutil.rmtree(out, ignore_errors=True)
    local = SRC / "node_modules" / ".bin" / ("tsc.cmd" if sys.platform == "win32" else "tsc")
    if local.exists():
        subprocess.run([str(local), "-p", str(SRC)], check=True)
    else:
        tsc = shutil.which("tsc")
        if not tsc:
            sys.exit("tsc not found: run `npm install` in vscode/ or install typescript globally")
        argv = [tsc, "--ignoreConfig", "--noResolve", "--strict", "--target", "ES2020", "--module", "commonjs",
                "--esModuleInterop", "--skipLibCheck", "--sourceMap", "false", "--outDir", str(out), str(SRC / "src" / "extension.ts")]
        # Without @types/vscode the compiler reports unresolved names but still emits.
        subprocess.run(argv, check=False, capture_output=True)
    js = out / "extension.js"
    if not js.exists():
        sys.exit("compilation produced no extension.js")
    return js


def main() -> None:
    js = compile_ts()
    shutil.rmtree(DEST, ignore_errors=True)
    (DEST / "out").mkdir(parents=True)
    (DEST / "media").mkdir()
    shutil.copy(js, DEST / "out" / "extension.js")
    shutil.copy(SRC / "package.json", DEST / "package.json")
    shutil.copy(SRC / "media" / "icon.svg", DEST / "media" / "icon.svg")
    shutil.copy(SRC / "README.md", DEST / "README.md")
    shutil.copy(ROOT / "LICENSE", DEST / "LICENSE.txt")
    (DEST / "__init__.py").write_text('"""Compiled VS Code extension, packaged by `pairshell install-vscode`."""\n')
    pkg = json.loads((DEST / "package.json").read_text(encoding="utf-8"))
    print(f"bundled {pkg['name']} {pkg['version']} -> {DEST.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
