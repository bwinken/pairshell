"""User-side attach: put *this* terminal into the shared tmux session.

* ssh:    ``ssh -t [opts] user@host "tmux new -A -s <session>"``
* telnet: built-in client with auto-login, then raw passthrough.  Ctrl-]
  disconnects (the tmux session survives).  On Windows the console is put
  in VT mode so arrow keys, colours and resizes work inside VS Code.
* local:  ``tmux new -A -s <session>`` right here (Linux/macOS).
"""

from __future__ import annotations

import codecs
import os
import shutil
import subprocess
import sys
import threading
from typing import Any

from .profiles import Profile
from .transports.base import AuthError, ByteStream, ConnectionLost, TransportError
from .transports.ssh import find_ssh, ssh_base_args, ssh_missing_hint
from .transports.telnet import TelnetSession, telnet_login

DETACH_KEY = b"\x1d"  # Ctrl-]
DEFAULT_TERM = "xterm-256color"


class AttachError(Exception):
    pass


def remote_tmux_command(session: str, force_term: bool) -> str:
    cmd = f"tmux new -A -s {session}"
    if force_term:
        cmd = f"env TERM={DEFAULT_TERM} " + cmd
    return cmd


def attach(profile: Profile, password: str | None = None) -> int:
    """Attach the current terminal.  Returns the exit code of the session."""
    if profile.protocol == "local":
        return _attach_local(profile)
    if profile.protocol == "ssh":
        return _attach_ssh(profile)
    if profile.protocol == "telnet":
        return TelnetAttach(profile, password or "").run()
    raise AttachError(f"unknown protocol {profile.protocol!r}")


def _attach_local(profile: Profile) -> int:
    tmux = shutil.which("tmux")
    if not tmux:
        raise AttachError("tmux not found on PATH")
    env = dict(os.environ)
    env.setdefault("TERM", DEFAULT_TERM)
    return subprocess.call([tmux, "new", "-A", "-s", profile.session], env=env)


def _attach_ssh(profile: Profile) -> int:
    exe = find_ssh()
    if not exe:
        raise AttachError(ssh_missing_hint())
    force_term = not os.environ.get("TERM")
    argv = (
        [exe, "-t"]
        + ssh_base_args(profile.host, profile.port, profile.user, profile.key_path or None, profile.ssh_options)
        + [remote_tmux_command(profile.session, force_term)]
    )
    return subprocess.call(argv)


# --------------------------------------------------------------------------
# Console abstraction for the built-in telnet client
# --------------------------------------------------------------------------


class _PosixConsole:
    def __init__(self) -> None:
        self.fd_in = sys.stdin.fileno()
        self.fd_out = sys.stdout.fileno()
        self._saved: Any = None

    def enter_raw(self) -> None:
        import termios
        import tty

        self._saved = termios.tcgetattr(self.fd_in)
        tty.setraw(self.fd_in)

    def leave_raw(self) -> None:
        if self._saved is not None:
            import termios

            termios.tcsetattr(self.fd_in, termios.TCSADRAIN, self._saved)
            self._saved = None

    def read_input(self) -> bytes:
        try:
            return os.read(self.fd_in, 4096)
        except OSError:
            return b""

    def write_bytes(self, data: bytes) -> None:
        view = memoryview(data)
        while view:
            n = os.write(self.fd_out, view)
            view = view[n:]

    def write_text(self, text: str) -> None:
        self.write_bytes(text.encode("utf-8", "replace"))

    def size(self) -> tuple[int, int]:
        s = shutil.get_terminal_size((80, 24))
        return s.columns, s.lines


class _WinConsole:  # pragma: no cover - Windows only
    ENABLE_PROCESSED_INPUT = 0x0001
    ENABLE_LINE_INPUT = 0x0002
    ENABLE_ECHO_INPUT = 0x0004
    ENABLE_VIRTUAL_TERMINAL_INPUT = 0x0200
    ENABLE_PROCESSED_OUTPUT = 0x0001
    ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
    DISABLE_NEWLINE_AUTO_RETURN = 0x0008

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self.ctypes = ctypes
        self.wintypes = wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.k32 = k32
        k32.GetStdHandle.restype = wintypes.HANDLE
        self.hin = k32.GetStdHandle(-10)
        self.hout = k32.GetStdHandle(-11)
        self._saved_in = wintypes.DWORD()
        self._saved_out = wintypes.DWORD()
        if not k32.GetConsoleMode(self.hin, ctypes.byref(self._saved_in)) or not k32.GetConsoleMode(
            self.hout, ctypes.byref(self._saved_out)
        ):
            raise AttachError("attach needs an interactive console (stdin/stdout must be a terminal)")
        self._saved_cp = k32.GetConsoleOutputCP()
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._raw = False

    def enter_raw(self) -> None:
        k32, wt = self.k32, self.wintypes
        new_in = (self._saved_in.value | self.ENABLE_VIRTUAL_TERMINAL_INPUT) & ~(
            self.ENABLE_LINE_INPUT | self.ENABLE_ECHO_INPUT | self.ENABLE_PROCESSED_INPUT
        )
        new_out = (
            self._saved_out.value
            | self.ENABLE_PROCESSED_OUTPUT
            | self.ENABLE_VIRTUAL_TERMINAL_PROCESSING
            | self.DISABLE_NEWLINE_AUTO_RETURN
        )
        k32.SetConsoleMode(self.hin, wt.DWORD(new_in))
        k32.SetConsoleMode(self.hout, wt.DWORD(new_out))
        k32.SetConsoleOutputCP(65001)
        self._raw = True

    def leave_raw(self) -> None:
        if self._raw:
            self.k32.SetConsoleMode(self.hin, self._saved_in)
            self.k32.SetConsoleMode(self.hout, self._saved_out)
            self.k32.SetConsoleOutputCP(self._saved_cp)
            self._raw = False

    def read_input(self) -> bytes:
        ctypes, wt = self.ctypes, self.wintypes
        buf = ctypes.create_unicode_buffer(1024)
        n = wt.DWORD()
        if not self.k32.ReadConsoleW(self.hin, buf, 1024, ctypes.byref(n), None):
            return b""
        return buf[: n.value].encode("utf-8", "replace")

    def write_text(self, text: str) -> None:
        ctypes, wt = self.ctypes, self.wintypes
        n = wt.DWORD()
        for i in range(0, len(text), 4096):
            chunk = text[i : i + 4096]
            self.k32.WriteConsoleW(self.hout, chunk, len(chunk), ctypes.byref(n), None)

    def write_bytes(self, data: bytes) -> None:
        text = self._decoder.decode(data)
        if text:
            self.write_text(text)

    def size(self) -> tuple[int, int]:
        ctypes, wt = self.ctypes, self.wintypes

        class COORD(ctypes.Structure):
            _fields_ = [("X", wt.SHORT), ("Y", wt.SHORT)]

        class SMALL_RECT(ctypes.Structure):
            _fields_ = [("Left", wt.SHORT), ("Top", wt.SHORT), ("Right", wt.SHORT), ("Bottom", wt.SHORT)]

        class CSBI(ctypes.Structure):
            _fields_ = [
                ("dwSize", COORD),
                ("dwCursorPosition", COORD),
                ("wAttributes", wt.WORD),
                ("srWindow", SMALL_RECT),
                ("dwMaximumWindowSize", COORD),
            ]

        info = CSBI()
        if self.k32.GetConsoleScreenBufferInfo(self.hout, ctypes.byref(info)):
            w = info.srWindow.Right - info.srWindow.Left + 1
            h = info.srWindow.Bottom - info.srWindow.Top + 1
            if w > 0 and h > 0:
                return w, h
        s = shutil.get_terminal_size((80, 24))
        return s.columns, s.lines


def make_console() -> Any:
    if sys.platform == "win32":
        return _WinConsole()
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise AttachError("attach needs an interactive terminal")
    return _PosixConsole()


# --------------------------------------------------------------------------
# Built-in telnet client
# --------------------------------------------------------------------------


class TelnetAttach:
    def __init__(self, profile: Profile, password: str) -> None:
        self.profile = profile
        self.password = password

    def run(self) -> int:
        p = self.profile
        console = make_console()
        term = os.environ.get("TERM") or DEFAULT_TERM
        size = console.size()
        session = TelnetSession(p.host, p.port, term=term, size=size)
        try:
            session.open()
        except TransportError as exc:
            console.write_text(f"{exc}\n")
            return 2
        stream = ByteStream(session.read_some, session.write, session.close, name="telnet").start()
        try:
            telnet_login(stream, p.user, self.password, on_output=console.write_bytes)
        except AuthError as exc:
            stream.close()
            console.write_text(f"\n[pairshell] {exc}. Update the password with `pairshell edit {p.name}`.\n")
            return 2
        except TransportError as exc:
            stream.close()
            console.write_text(f"\n[pairshell] {exc}\n")
            return 2
        console.write_bytes(stream.drain())
        # Shell-agnostic: works for tcsh and sh-family alike, and `exec` makes
        # the connection close when the tmux client exits.
        stream.write(f"exec env TERM={term} tmux new -A -s {p.session}\n".encode())
        console.write_text(
            f"\r\n[pairshell] attached to {p.name}; press Ctrl-] to disconnect (the tmux session keeps running)."
            f"\r\n[pairshell] If a plain shell prompt shows instead of tmux, type: tmux new -A -s {p.session}\r\n"
        )
        console.enter_raw()
        try:
            self._passthrough(stream, session, console, size)
        finally:
            console.leave_raw()
            stream.close()
        console.write_text(f"\r\n[pairshell] disconnected from {p.name}; tmux session '{p.session}' is still running on {p.host}.\r\n")
        return 0

    @staticmethod
    def _passthrough(stream: ByteStream, session: TelnetSession, console: Any, size: tuple[int, int]) -> None:
        stop = threading.Event()

        def pump_input() -> None:
            while not stop.is_set():
                data = console.read_input()
                if not data:
                    if stop.is_set():
                        return
                    continue
                if DETACH_KEY in data:
                    head = data[: data.index(DETACH_KEY)]
                    try:
                        if head:
                            stream.write(head)
                    finally:
                        stream.close()
                    return
                try:
                    stream.write(data)
                except ConnectionLost:
                    return

        threading.Thread(target=pump_input, name="stdin", daemon=True).start()
        last = size
        try:
            while True:
                try:
                    data = stream.read_any(0.5)
                except ConnectionLost:
                    break
                if data:
                    console.write_bytes(data)
                cur = console.size()
                if cur != last:
                    last = cur
                    session.send_naws(cur)
        finally:
            stop.set()


__all__ = ["AttachError", "TelnetAttach", "attach", "remote_tmux_command"]
