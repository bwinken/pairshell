"""Transport interface and the shared control-channel protocol.

A transport gives us a *control shell*: a ``bash --norc --noprofile`` on the
remote host that nobody but pairshell can see.  Every control command is
followed by::

    printf '\\n__CTL_%s_<nonce>__\\n' "$?"

and we read the byte stream until that sentinel shows up.  The protocol is
identical for Telnet (a pty) and SSH (pipes); only the way bytes travel
differs, so subclasses implement :meth:`StreamTransport._open` and the
small :class:`ByteStream` hooks.
"""

from __future__ import annotations

import abc
import logging
import re
import secrets
import threading
import time
from typing import Callable

log = logging.getLogger("pairshell.transport")


class TransportError(Exception):
    """Base class for transport failures."""


class AuthError(TransportError):
    """The remote rejected our credentials."""


class ConnectionLost(TransportError):
    """The control connection hit EOF or a socket/pipe error."""


class ControlTimeout(TransportError):
    """A control command did not produce its sentinel in time.

    The remote state is unknown afterwards, so the caller drops the
    connection and logs in again on the next call.
    """


# --------------------------------------------------------------------------
# Byte stream with a background reader
# --------------------------------------------------------------------------


class ByteStream:
    """A duplex byte stream with a reader thread and regex-driven reads.

    ``read_fn()`` must block until at least one byte is available and return
    ``b""`` (or raise ``EOFError``/``OSError``) at end of stream.
    ``write_fn(data)`` must write all bytes.  ``close_fn()`` tears the
    underlying connection down and must make ``read_fn`` return/raise.
    """

    def __init__(
        self,
        read_fn: Callable[[], bytes],
        write_fn: Callable[[bytes], None],
        close_fn: Callable[[], None],
        name: str = "stream",
    ) -> None:
        self._read_fn = read_fn
        self._write_fn = write_fn
        self._close_fn = close_fn
        self.name = name
        self._buf = bytearray()
        self._cond = threading.Condition()
        self._eof = False
        self._error: BaseException | None = None
        self._closed = False
        self._wlock = threading.Lock()
        self._thread = threading.Thread(target=self._reader, name=f"{name}-reader", daemon=True)

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> "ByteStream":
        self._thread.start()
        return self

    def _reader(self) -> None:
        try:
            while True:
                data = self._read_fn()
                if not data:
                    break
                with self._cond:
                    self._buf.extend(data)
                    self._cond.notify_all()
        except (EOFError, OSError, ValueError) as exc:
            with self._cond:
                if not self._closed:
                    self._error = exc
        except Exception as exc:  # pragma: no cover - defensive
            with self._cond:
                self._error = exc
        with self._cond:
            self._eof = True
            self._cond.notify_all()

    def close(self) -> None:
        with self._cond:
            if self._closed:
                return
            self._closed = True
        try:
            self._close_fn()
        except Exception:  # pragma: no cover - best effort
            pass
        with self._cond:
            self._eof = True
            self._cond.notify_all()

    @property
    def eof(self) -> bool:
        with self._cond:
            return self._eof

    @property
    def closed(self) -> bool:
        return self._closed

    # -- I/O ---------------------------------------------------------------

    def write(self, data: bytes) -> None:
        if self._closed:
            raise ConnectionLost(f"{self.name}: stream is closed")
        try:
            with self._wlock:
                self._write_fn(data)
        except (OSError, ValueError, BrokenPipeError) as exc:
            raise ConnectionLost(f"{self.name}: write failed: {exc}") from exc

    def drain(self) -> bytes:
        """Return and discard everything buffered so far."""
        with self._cond:
            data = bytes(self._buf)
            del self._buf[:]
            return data

    def peek(self) -> bytes:
        with self._cond:
            return bytes(self._buf)

    def read_until(self, pattern: "re.Pattern[bytes]", timeout: float) -> tuple[bytes, "re.Match[bytes]"]:
        """Wait until ``pattern`` matches the buffer.

        Returns ``(data_before_match, match)`` and consumes everything up to
        and including the match.  Raises :class:`ControlTimeout` when the
        deadline passes and :class:`ConnectionLost` on EOF.
        """
        deadline = time.monotonic() + timeout
        search_from = 0
        with self._cond:
            while True:
                # Search an immutable snapshot: a match object over the live
                # bytearray would be a view that breaks once we delete from it.
                snapshot = bytes(self._buf)
                m = pattern.search(snapshot, search_from)
                if m:
                    before = snapshot[: m.start()]
                    del self._buf[: m.end()]
                    return before, m
                if self._eof:
                    tail = bytes(self._buf)
                    del self._buf[:]
                    msg = f"{self.name}: connection closed while waiting for output"
                    if self._error:
                        msg += f": {self._error}"
                    if tail:
                        msg += f"; last output: {tail[-400:]!r}"
                    raise ConnectionLost(msg)
                # Allow the pattern to span chunk boundaries by re-searching a bit back.
                search_from = max(0, len(self._buf) - 256)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ControlTimeout(f"{self.name}: timed out after {timeout:.1f}s waiting for sentinel")
                self._cond.wait(remaining)

    def read_any(self, timeout: float) -> bytes:
        """Return whatever is buffered, waiting up to ``timeout`` for data.

        Returns ``b""`` on timeout; raises :class:`ConnectionLost` at EOF once
        the buffer is empty.
        """
        deadline = time.monotonic() + timeout
        with self._cond:
            while not self._buf:
                if self._eof:
                    raise ConnectionLost(f"{self.name}: connection closed")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return b""
                self._cond.wait(remaining)
            data = bytes(self._buf)
            del self._buf[:]
            return data

    def wait_quiet(
        self,
        min_quiet: float,
        max_total: float,
        stop_pattern: "re.Pattern[bytes] | None" = None,
    ) -> tuple[bytes, bool]:
        """Collect bytes until the stream is quiet for ``min_quiet`` seconds.

        Stops early when ``stop_pattern`` matches or after ``max_total``
        seconds.  Returns ``(collected_bytes, pattern_matched)``.  The
        collected bytes are consumed from the buffer.
        """
        start = time.monotonic()
        last_change = start
        seen = 0
        with self._cond:
            while True:
                now = time.monotonic()
                if len(self._buf) != seen:
                    seen = len(self._buf)
                    last_change = now
                    if stop_pattern is not None and stop_pattern.search(bytes(self._buf)):
                        data = bytes(self._buf)
                        del self._buf[:]
                        return data, True
                if self._eof or (now - last_change >= min_quiet and seen > 0) or now - start >= max_total:
                    data = bytes(self._buf)
                    del self._buf[:]
                    return data, False
                self._cond.wait(min(0.1, max(0.01, min_quiet - (now - last_change))))


# --------------------------------------------------------------------------
# Transport interface
# --------------------------------------------------------------------------


class Transport(abc.ABC):
    """Runs shell commands on the remote host through a persistent channel."""

    name = "base"

    @abc.abstractmethod
    def connect(self) -> None:
        """Open the connection and log in.  Idempotent when already connected."""

    @abc.abstractmethod
    def run(self, cmd: str, timeout: float = 15.0) -> tuple[int, str]:
        """Run ``cmd`` in the control shell and return ``(exit_code, output)``."""

    @abc.abstractmethod
    def close(self) -> None:
        """Drop the connection.  Never touches the remote tmux session."""

    @property
    @abc.abstractmethod
    def connected(self) -> bool: ...

    def describe(self) -> str:
        return self.name


def ctl_sentinel_pattern(nonce: str) -> "re.Pattern[bytes]":
    return re.compile(rb"__CTL_(\d+)_" + nonce.encode("ascii") + rb"__")


def wrap_control_command(cmd: str, nonce: str) -> bytes:
    """Build the bytes sent to the control shell for one command.

    The command runs in a subshell so a stray ``exit``/``cd``/``exec`` cannot
    take the persistent control shell down, with stdin from ``/dev/null`` so
    nothing it starts can swallow the commands queued behind it, and with
    stderr folded into stdout so error text comes back with the output.
    """
    return (
        "( " + cmd + "\n) </dev/null 2>&1\n" + "printf '\\n__CTL_%s_" + nonce + "__\\n' \"$?\"\n"
    ).encode("utf-8", "surrogateescape")


class StreamTransport(Transport):
    """Common implementation of the sentinel protocol over a :class:`ByteStream`.

    Subclasses implement :meth:`_open` (connect + log in, returning a stream
    positioned at a freshly started ``bash --norc --noprofile``) and may
    override :attr:`has_tty` and :meth:`_setup_line`.
    """

    #: True when the remote bash runs on a pty (Telnet); False for pipes (SSH).
    has_tty = False
    #: Seconds to wait for the sync command after login.
    sync_timeout = 12.0
    def __init__(self) -> None:
        self._stream: ByteStream | None = None
        self._lock = threading.RLock()
        self.connect_count = 0
        self.last_connect_error: str | None = None

    # -- to be provided by subclasses ---------------------------------------

    @abc.abstractmethod
    def _open(self) -> ByteStream:
        """Connect, authenticate and start ``bash --norc --noprofile``."""

    def _setup_line(self) -> str:
        """The line that quiets the control shell down before use."""
        parts = []
        if self.has_tty:
            parts.append("stty -echo 2>/dev/null")
        parts += [
            "PS1=''",
            "PS2=''",
            "unset HISTFILE PROMPT_COMMAND MAIL MAILPATH",
            "unset TMOUT 2>/dev/null",
            "MAILCHECK=-1",
            "set +m +H",
            "set +o history 2>/dev/null",
        ]
        if self.has_tty:
            parts.append("bind 'set enable-bracketed-paste off' 2>/dev/null")
        parts += [
            "export TERM=dumb",
            "if locale -a 2>/dev/null | grep -qiE '^en_US\\.utf-?8$'; then export LANG=en_US.UTF-8; else export LANG=C.utf8; fi",
            "unset LC_ALL",
        ]
        return "; ".join(parts)

    # -- Transport API -------------------------------------------------------

    @property
    def connected(self) -> bool:
        s = self._stream
        return s is not None and not s.eof and not s.closed

    def connect(self) -> None:
        with self._lock:
            if self.connected:
                return
            self.close()
            try:
                stream = self._open()
            except TransportError as exc:
                self.last_connect_error = str(exc)
                raise
            self._stream = stream
            try:
                self._initialise(stream)
            except TransportError as exc:
                self.last_connect_error = str(exc)
                self.close()
                raise
            self.connect_count += 1
            self.last_connect_error = None
            log.info("%s: control channel ready", self.name)

    def _initialise(self, stream: ByteStream) -> None:
        """Quiet the control shell down and synchronise on a sentinel.

        ``_open`` leaves us right after ``exec bash --norc --noprofile``.  A
        csh login shell may flush typeahead while it execs, so on a pty we
        wait for the output to settle before sending the setup line.  If the
        sync still fails, we re-exec bash and try once more.
        """
        last_exc: TransportError | None = None
        for attempt in (1, 2):
            if attempt == 2:
                stream.write(b"exec bash --norc --noprofile\n")
            if self.has_tty or attempt == 2:
                stream.wait_quiet(min_quiet=1.0, max_total=3.0)
            stream.drain()
            stream.write((self._setup_line() + "\n").encode())
            try:
                self._run_once(stream, ":", self.sync_timeout)
                return
            except ControlTimeout as exc:
                last_exc = exc
                log.warning("%s: sync attempt %d timed out", self.name, attempt)
            except ConnectionLost as exc:
                raise ConnectionLost(f"{self.name}: connection closed during setup ({exc})") from None
        raise ControlTimeout(f"{self.name}: control shell did not respond to setup ({last_exc})")

    def close(self) -> None:
        with self._lock:
            s = self._stream
            self._stream = None
            if s is not None:
                s.close()

    def run(self, cmd: str, timeout: float = 15.0) -> tuple[int, str]:
        if "\0" in cmd:
            raise TransportError("control command must not contain NUL bytes")
        with self._lock:
            for attempt in (1, 2):
                if not self.connected:
                    self.connect()
                stream = self._stream
                assert stream is not None
                try:
                    return self._run_once(stream, cmd, timeout)
                except ConnectionLost as exc:
                    log.warning("%s: connection lost (%s); %s", self.name, exc, "re-logging in" if attempt == 1 else "giving up")
                    self.close()
                    if attempt == 2:
                        raise
                except ControlTimeout:
                    # State unknown: drop the connection, re-login on the next call.
                    self.close()
                    raise
        raise ConnectionLost("unreachable")  # pragma: no cover

    def _run_once(self, stream: ByteStream, cmd: str, timeout: float) -> tuple[int, str]:
        nonce = secrets.token_hex(4)
        stream.drain()
        stream.write(wrap_control_command(cmd, nonce))
        before, m = stream.read_until(ctl_sentinel_pattern(nonce), timeout)
        rc = int(m.group(1))
        text = before.decode("utf-8", "replace").replace("\r\n", "\n").replace("\r", "")
        # The sentinel is preceded by a newline of its own; drop just that one.
        if text.endswith("\n"):
            text = text[:-1]
        return rc, text


__all__ = [
    "AuthError",
    "ByteStream",
    "ConnectionLost",
    "ControlTimeout",
    "StreamTransport",
    "Transport",
    "TransportError",
    "ctl_sentinel_pattern",
    "wrap_control_command",
]
