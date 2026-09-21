"""A tiny telnet daemon for tests (POSIX only).

Behaves like a classic Linux telnetd in kludge line mode: negotiates
TTYPE/NAWS/ECHO/SGA, prompts ``login:`` / ``Password:`` (echoing the user
name, not the password), prints a banner containing the words "write error"
(to make sure the auth-failure regex stays narrow), then runs a login shell
on a pty and relays bytes.  ``Login incorrect`` on bad credentials.
"""

from __future__ import annotations

import fcntl
import os
import pty
import select
import shutil
import signal
import socket
import struct
import termios
import threading
import time

IAC, DONT, DO, WONT, WILL, SB, SE = 255, 254, 253, 252, 251, 250, 240
OPT_ECHO, OPT_SGA, OPT_TTYPE, OPT_NAWS = 1, 3, 24, 31

BANNER = (
    b"Last login: Mon Sep 21 09:00:00 from 192.0.2.10\r\n"
    b"Welcome to example-host. Note: a write error in /var/log is harmless here.\r\n"
)


class TelnetParser:
    """Splits incoming bytes into application data and telnet commands."""

    def __init__(self) -> None:
        self.state = "data"
        self.cmd = 0
        self.sb = bytearray()
        self.events: list[tuple] = []
        self.prev_cr = False

    def feed(self, data: bytes) -> bytes:
        out = bytearray()
        for b in data:
            st = self.state
            if st == "data":
                if b == IAC:
                    self.state = "iac"
                elif self.prev_cr and b in (0, 10):
                    self.prev_cr = False  # CR LF / CR NUL -> CR
                else:
                    self.prev_cr = b == 13
                    out.append(b)
            elif st == "iac":
                if b == IAC:
                    out.append(IAC)
                    self.state = "data"
                elif b in (DO, DONT, WILL, WONT):
                    self.cmd = b
                    self.state = "opt"
                elif b == SB:
                    self.sb = bytearray()
                    self.state = "sb"
                else:
                    self.events.append(("cmd", b))
                    self.state = "data"
            elif st == "opt":
                self.events.append((self.cmd, b))
                self.state = "data"
            elif st == "sb":
                if b == IAC:
                    self.state = "sb_iac"
                else:
                    self.sb.append(b)
            elif st == "sb_iac":
                if b == SE:
                    self.events.append(("sb", bytes(self.sb)))
                    self.state = "data"
                else:
                    self.sb.append(b)
                    self.state = "sb"
        return bytes(out)


class FakeTelnetd:
    def __init__(self, user: str = "alice", password: str = "secret", shell: list[str] | None = None, home: str | None = None) -> None:
        self.user = user
        self.password = password
        self.shell = shell or [shutil.which("tcsh") or shutil.which("bash") or "sh"]
        self.home = home or os.environ.get("HOME", "/tmp")
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._children: list[int] = []
        self.logins: list[tuple[str, str]] = []
        self.term_types: list[bytes] = []
        self.naws: list[tuple[int, int]] = []
        self.thread = threading.Thread(target=self._accept_loop, daemon=True)
        self.thread.start()

    # -- lifecycle -------------------------------------------------------------

    def close(self) -> None:
        self._stop.set()
        try:
            self.sock.close()
        except OSError:
            pass
        for pid in self._children:
            try:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
            except OSError:
                pass

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                r, _, _ = select.select([self.sock], [], [], 0.2)
            except (OSError, ValueError):
                return
            if not r:
                continue
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            t = threading.Thread(target=self._session, args=(conn,), daemon=True)
            t.start()
            self._threads.append(t)

    # -- one connection ------------------------------------------------------

    def _session(self, conn: socket.socket) -> None:
        parser = TelnetParser()
        term = b"dumb"
        size = (80, 24)
        pending = bytearray()

        def send(data: bytes) -> None:
            conn.sendall(data.replace(bytes([IAC]), bytes([IAC, IAC])))

        def handle_events() -> None:
            nonlocal term, size
            while parser.events:
                ev = parser.events.pop(0)
                if ev[0] == WILL and ev[1] == OPT_TTYPE:
                    conn.sendall(bytes([IAC, SB, OPT_TTYPE, 1, IAC, SE]))  # SEND
                elif ev[0] == "sb":
                    payload = ev[1]
                    if payload[:2] == bytes([OPT_TTYPE, 0]):
                        term = payload[2:]
                        self.term_types.append(term)
                    elif payload[:1] == bytes([OPT_NAWS]) and len(payload) >= 5:
                        cols, rows = struct.unpack(">HH", payload[1:5])
                        size = (cols, rows)
                        self.naws.append(size)

        def read_more(timeout: float = 30.0) -> bytes:
            r, _, _ = select.select([conn], [], [], timeout)
            if not r:
                raise TimeoutError
            data = conn.recv(4096)
            if not data:
                raise EOFError
            app = parser.feed(data)
            handle_events()
            return app

        def read_line(echo: bool) -> str:
            nonlocal pending
            while True:
                for i, b in enumerate(pending):
                    if b in (10, 13):
                        line = bytes(pending[:i])
                        del pending[: i + 1]
                        if echo:
                            conn.sendall(b"\r\n")
                        return line.decode("utf-8", "replace")
                if echo and pending:
                    conn.sendall(bytes(pending))
                    # keep the bytes but remember they were echoed
                    echoed = bytes(pending)
                    del pending[:]
                    more = read_more()
                    pending = bytearray(echoed) + more
                    # avoid re-echoing what we already sent
                    if more:
                        conn.sendall(more.replace(b"\n", b"").replace(b"\r", b""))
                    for i, b in enumerate(pending):
                        if b in (10, 13):
                            line = bytes(pending[:i])
                            del pending[: i + 1]
                            conn.sendall(b"\r\n")
                            return line.decode("utf-8", "replace")
                    continue
                pending += read_more()

        try:
            # Real telnetd opens with its option requests and waits a little.
            conn.sendall(bytes([IAC, DO, OPT_TTYPE, IAC, DO, OPT_NAWS, IAC, WILL, OPT_ECHO, IAC, WILL, OPT_SGA]))
            time.sleep(0.3)
            try:
                pending += parser.feed(conn.recv(4096, socket.MSG_DONTWAIT))
                handle_events()
            except (BlockingIOError, OSError):
                pass
            ok = False
            for _ in range(3):
                send(b"\r\nexample-host login: ")
                user = read_line(echo=True)
                send(b"Password: ")
                pw = read_line(echo=False)
                send(b"\r\n")
                self.logins.append((user, pw))
                if user == self.user and pw == self.password:
                    ok = True
                    break
                time.sleep(0.2)
                send(b"Login incorrect\r\n")
            if not ok:
                conn.close()
                return
            send(BANNER)
            pid, fd = pty.fork()
            if pid == 0:  # child: the login shell
                os.environ["TERM"] = term.decode("ascii", "replace") or "dumb"
                os.environ["HOME"] = self.home
                os.environ["SHELL"] = self.shell[0]
                os.chdir(self.home)
                try:
                    os.execvp(self.shell[0], self.shell)
                finally:
                    os._exit(127)
            self._children.append(pid)
            fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", size[1], size[0], 0, 0))
            if pending:
                os.write(fd, bytes(pending))
                pending = bytearray()
            while not self._stop.is_set():
                r, _, _ = select.select([conn, fd], [], [], 0.5)
                if conn in r:
                    data = conn.recv(4096)
                    if not data:
                        break
                    app = parser.feed(data)
                    handle_events()
                    for ev_size in self.naws[-1:]:
                        if ev_size != size:
                            size = ev_size
                            fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", size[1], size[0], 0, 0))
                    if app:
                        os.write(fd, app)
                if fd in r:
                    try:
                        out = os.read(fd, 4096)
                    except OSError:
                        break
                    if not out:
                        break
                    send(out)
            try:
                os.kill(pid, signal.SIGHUP)
            except OSError:
                pass
        except (EOFError, TimeoutError, OSError):
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass


__all__ = ["FakeTelnetd", "TelnetParser"]
