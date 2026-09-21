"""SSH transport: ``ssh.exe`` with pipes, no PTY, same sentinel protocol.

Key authentication is the supported mode.  ``ssh.exe`` has no way to take
a password non-interactively (no ``sshpass`` on Windows, and ``BatchMode``
disables the prompt on purpose), so password auth is not offered in v1.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys

from .base import AuthError, ByteStream, ConnectionLost, StreamTransport, TransportError

log = logging.getLogger("pairshell.ssh")


def find_ssh() -> str | None:
    """Locate the OpenSSH client; on Windows also look in the System32 folder."""
    exe = shutil.which("ssh")
    if exe:
        return exe
    if sys.platform == "win32":
        candidate = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "OpenSSH", "ssh.exe")
        if os.path.exists(candidate):
            return candidate
    return None


def ssh_missing_hint() -> str:
    if sys.platform == "win32":
        return (
            "ssh.exe not found. Install the Windows 'OpenSSH Client' optional feature "
            "(Settings > Apps > Optional features) or add ssh.exe to PATH."
        )
    return "ssh not found on PATH."


def ssh_base_args(host: str, port: int, user: str, key_path: str | None, ssh_options: list[str]) -> list[str]:
    """Common ``ssh`` arguments (without the client-mode flags)."""
    args: list[str] = []
    if port and int(port) != 22:
        args += ["-p", str(int(port))]
    if key_path:
        args += ["-i", os.path.expanduser(key_path)]
    args += list(ssh_options)
    args.append(f"{user}@{host}" if user else host)
    return args


class PipeTransport(StreamTransport):
    """Base for transports that talk to a local child process over pipes."""

    has_tty = False

    def __init__(self) -> None:
        super().__init__()
        self._proc: subprocess.Popen[bytes] | None = None

    def _argv(self) -> list[str]:
        raise NotImplementedError

    def _open(self) -> ByteStream:
        argv = self._argv()
        creationflags = 0
        if sys.platform == "win32":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=0,
                creationflags=creationflags,
            )
        except OSError as exc:
            raise TransportError(f"{self.name}: cannot start {argv[0]}: {exc}") from exc
        self._proc = proc
        assert proc.stdout is not None and proc.stdin is not None
        out_fd = proc.stdout.fileno()

        def read() -> bytes:
            try:
                return os.read(out_fd, 65536)
            except OSError:
                return b""

        def write(data: bytes) -> None:
            proc.stdin.write(data)  # type: ignore[union-attr]
            proc.stdin.flush()  # type: ignore[union-attr]

        def close() -> None:
            for f in (proc.stdin, proc.stdout):
                try:
                    if f is not None:
                        f.close()
                except OSError:
                    pass
            if proc.poll() is None:
                try:
                    proc.kill()
                except OSError:
                    pass
            try:
                proc.wait(timeout=2)
            except Exception:
                pass

        return ByteStream(read, write, close, name=self.name).start()

    def _initialise(self, stream: ByteStream) -> None:
        try:
            super()._initialise(stream)
        except ConnectionLost as exc:
            proc = self._proc
            rc = proc.poll() if proc is not None else None
            text = str(exc)
            lowered = text.lower()
            if "permission denied" in lowered or "authentication" in lowered or "host key verification failed" in lowered:
                raise AuthError(f"{self.name}: authentication failed: {text}") from None
            raise ConnectionLost(f"{self.name}: process exited (rc={rc}): {text}") from None


class SshTransport(PipeTransport):
    name = "ssh"

    def __init__(self, host: str, port: int, user: str, key_path: str | None = None, ssh_options: list[str] | None = None) -> None:
        super().__init__()
        self.host = host
        self.port = port
        self.user = user
        self.key_path = key_path
        self.ssh_options = list(ssh_options or [])

    def describe(self) -> str:
        return f"ssh {self.user}@{self.host}:{self.port}"

    def _argv(self) -> list[str]:
        exe = find_ssh()
        if not exe:
            raise TransportError(ssh_missing_hint())
        return (
            [exe, "-T", "-o", "BatchMode=yes", "-o", "ServerAliveInterval=60", "-o", "ServerAliveCountMax=3"]
            + ssh_base_args(self.host, self.port, self.user, self.key_path, self.ssh_options)
            + ["bash", "--norc", "--noprofile"]
        )
