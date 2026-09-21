"""Local transport: the "remote" is this machine (Linux/macOS only).

Handy for trying pairshell without a server and for the integration tests;
it is not available on Windows because it needs a local bash and tmux.
"""

from __future__ import annotations

import shutil
import sys

from .base import TransportError
from .ssh import PipeTransport


class LocalTransport(PipeTransport):
    name = "local"

    def describe(self) -> str:
        return "local bash"

    def _argv(self) -> list[str]:
        if sys.platform == "win32":
            raise TransportError("the 'local' protocol needs a local bash and tmux; it is not available on Windows")
        bash = shutil.which("bash")
        if not bash:
            raise TransportError("bash not found on PATH")
        return [bash, "--norc", "--noprofile"]
