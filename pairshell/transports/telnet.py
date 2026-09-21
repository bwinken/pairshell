"""Telnet transport: one persistent connection holding the control shell.

Login flow (see README "Remote requirements"):

* ``login:`` -> user, ``Password:`` -> password, then wait for the shell
  (``Last login`` or a quiet line, at most 10 s).
* Auth failure is detected only with a narrow regex, because MOTDs
  routinely contain words like "error" or "write error".
* ``exec bash --norc --noprofile``; a csh login shell may flush typeahead
  across the exec, so the base class waits for output to settle before it
  sends the setup line.
"""

from __future__ import annotations

import logging
import re
import struct
import threading
import time
from typing import Callable

from . import _telnetlib as tl
from .base import AuthError, ByteStream, ConnectionLost, ControlTimeout, StreamTransport, TransportError

log = logging.getLogger("pairshell.telnet")

LOGIN_PROMPT = re.compile(rb"(?i)(login|username)\s*:\s*$")
PASSWORD_PROMPT = re.compile(rb"(?i)password\s*:\s*$")
AUTH_FAILURE = re.compile(rb"(?i)login incorrect|authentication failure|access denied|login failed")
LAST_LOGIN = re.compile(rb"Last login")
LOGIN_TIMEOUT = 25.0
PASSWORD_TIMEOUT = 15.0
SHELL_SETTLE = 10.0


class TelnetSession:
    """A negotiated Telnet connection (shared by the transport and ``attach``).

    Handles option negotiation the way real clients do: we agree to send
    the terminal type (``TTYPE``) and window size (``NAWS``), accept the
    server echoing and suppressing go-ahead, and refuse everything else.
    """

    def __init__(self, host: str, port: int, term: str = "xterm-256color", size: tuple[int, int] | None = None) -> None:
        self.host = host
        self.port = port
        self.term = term
        self.size = size  # (cols, rows)
        self.tn = tl.Telnet()
        self.tn.set_option_negotiation_callback(self._negotiate)
        self._naws_enabled = False
        self.on_negotiate: Callable[[bytes, bytes], None] | None = None

    # -- connection ----------------------------------------------------------

    def open(self, timeout: float = 15.0) -> None:
        try:
            self.tn.open(self.host, self.port, timeout)
        except OSError as exc:
            raise ConnectionLost(f"telnet: cannot connect to {self.host}:{self.port}: {exc}") from exc
        # Blocking reads from now on; timeouts are enforced by ByteStream.
        self.tn.sock.settimeout(None)

    def close(self) -> None:
        self.tn.close()

    def read_some(self) -> bytes:
        try:
            return self.tn.read_some()
        except EOFError:
            return b""

    def write(self, data: bytes) -> None:
        self.tn.write(data)

    # -- negotiation ---------------------------------------------------------

    def _negotiate(self, sock, cmd: bytes, opt: bytes) -> None:
        tn = self.tn
        if cmd == tl.DO:
            if opt == tl.TTYPE:
                tn.send_raw(tl.IAC + tl.WILL + opt)
            elif opt == tl.NAWS:
                self._naws_enabled = True
                tn.send_raw(tl.IAC + tl.WILL + opt)
                self.send_naws()
            elif opt == tl.SGA:
                tn.send_raw(tl.IAC + tl.WILL + opt)
            else:
                tn.send_raw(tl.IAC + tl.WONT + opt)
        elif cmd == tl.DONT:
            if opt == tl.NAWS:
                self._naws_enabled = False
            tn.send_raw(tl.IAC + tl.WONT + opt)
        elif cmd == tl.WILL:
            if opt in (tl.ECHO, tl.SGA, tl.BINARY):
                tn.send_raw(tl.IAC + tl.DO + opt)
            else:
                tn.send_raw(tl.IAC + tl.DONT + opt)
        elif cmd == tl.WONT:
            tn.send_raw(tl.IAC + tl.DONT + opt)
        elif cmd == tl.SE:
            data = tn.read_sb_data()
            if data[:2] == tl.TTYPE + b"\x01":  # TTYPE SEND
                tn.send_raw(tl.IAC + tl.SB + tl.TTYPE + b"\x00" + self.term.encode("ascii") + tl.IAC + tl.SE)
        if self.on_negotiate is not None:
            self.on_negotiate(cmd, opt)

    def send_naws(self, size: tuple[int, int] | None = None) -> None:
        """Tell the server our window size (columns, rows)."""
        if size is not None:
            self.size = size
        if not self._naws_enabled or not self.size:
            return
        cols, rows = self.size
        payload = struct.pack(">HH", max(1, cols), max(1, rows)).replace(tl.IAC, tl.IAC + tl.IAC)
        try:
            self.tn.send_raw(tl.IAC + tl.SB + tl.NAWS + payload + tl.IAC + tl.SE)
        except OSError:
            pass


def telnet_login(
    stream: ByteStream,
    user: str,
    password: str,
    on_output: Callable[[bytes], None] | None = None,
) -> bytes:
    """Drive the ``login:``/``Password:`` dialogue on an open stream.

    Returns the banner/MOTD bytes seen after authentication.  Raises
    :class:`AuthError` when the remote says the credentials are wrong.
    ``on_output`` receives every chunk we consumed (used by ``attach`` to
    show the banner to the user).  The password is never logged.
    """

    def emit(data: bytes) -> None:
        if on_output is not None and data:
            on_output(data)

    try:
        before, _ = stream.read_until(LOGIN_PROMPT, LOGIN_TIMEOUT)
    except ControlTimeout:
        tail = stream.drain()
        raise TransportError(
            "telnet: no login prompt within %ds%s" % (LOGIN_TIMEOUT, f" (got {tail[-200:]!r})" if tail else "")
        ) from None
    emit(before + b"login: ")
    stream.write(user.encode("utf-8") + b"\n")
    try:
        before, _ = stream.read_until(PASSWORD_PROMPT, PASSWORD_TIMEOUT)
        emit(before + b"Password: ")
        stream.write(password.encode("utf-8") + b"\n")
    except ControlTimeout:
        # Some accounts log in without a password; carry on.
        emit(stream.drain())

    data, matched = stream.wait_quiet(min_quiet=1.0, max_total=SHELL_SETTLE, stop_pattern=AUTH_FAILURE)
    if matched or AUTH_FAILURE.search(data):
        emit(data)
        raise AuthError("telnet: login failed (remote reported bad credentials)")
    emit(data)
    if LAST_LOGIN.search(data) is None and LOGIN_PROMPT.search(data.rstrip()) is not None:
        raise AuthError("telnet: login failed (login prompt came back)")
    return data


class TelnetTransport(StreamTransport):
    name = "telnet"
    has_tty = True

    def __init__(self, host: str, port: int, user: str, password: str) -> None:
        super().__init__()
        self.host = host
        self.port = port
        self.user = user
        self._password = password
        self._session: TelnetSession | None = None

    def describe(self) -> str:
        return f"telnet {self.user}@{self.host}:{self.port}"

    def _open(self) -> ByteStream:
        session = TelnetSession(self.host, self.port, term="dumb", size=(200, 50))
        session.open()
        stream = ByteStream(session.read_some, session.write, session.close, name="telnet").start()
        try:
            telnet_login(stream, self.user, self._password)
            stream.write(b"exec bash --norc --noprofile\n")
        except TransportError:
            stream.close()
            raise
        self._session = session
        return stream
