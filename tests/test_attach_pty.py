"""End-to-end test of the built-in telnet attach client through a pty.

Spawns ``pairshell attach`` under a pseudo-terminal against the fake
telnetd: auto-login, ``exec env TERM=... tmux new -A``, raw passthrough
with the tmux status line visible, then Ctrl-] to disconnect while the tmux
session survives.  POSIX only (the Windows console path cannot run here).
"""

from __future__ import annotations

import os
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

POSIX = sys.platform != "win32"
HAVE_TMUX = POSIX and bool(shutil.which("tmux")) and bool(shutil.which("bash"))
ROOT = Path(__file__).resolve().parent.parent


def read_until(fd: int, needle: bytes, timeout: float) -> bytes:
    buf = b""
    deadline = time.monotonic() + timeout
    while needle not in buf:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError(f"{needle!r} not seen within {timeout}s; got {buf[-800:]!r}")
        r, _, _ = select.select([fd], [], [], min(remaining, 0.5))
        if not r:
            continue
        try:
            chunk = os.read(fd, 65536)
        except OSError:
            break
        if not chunk:
            break
        buf += chunk
    return buf


@unittest.skipUnless(HAVE_TMUX, "needs tmux, bash and a pty (POSIX)")
class TelnetAttachPtyTests(unittest.TestCase):
    def setUp(self):
        import fcntl
        import struct
        import termios

        from tests.fake_telnetd import FakeTelnetd

        self.tmp = tempfile.mkdtemp(prefix="pairshell-attach-")
        self.session = f"psatt_{os.getpid()}_{int(time.time() * 1000) % 100000}"
        shell = [shutil.which("tcsh")] if shutil.which("tcsh") else [shutil.which("bash"), "--norc", "--noprofile", "-i"]
        self.daemon = FakeTelnetd(user="alice", password="pw", shell=shell, home=self.tmp)
        self.env = dict(
            os.environ,
            PAIRSHELL_HOME=self.tmp,
            HOME=self.tmp,
            PAIRSHELL_PASSWORD="pw",
            TERM="xterm",
        )
        # The shell behind the fake telnetd inherits this process's
        # environment, so tmux lives on the default socket for this uid.
        self._fcntl, self._struct, self._termios = fcntl, struct, termios
        r = subprocess.run(
            [sys.executable, "-m", "pairshell", "add", "tel", "--protocol", "telnet", "--host", "127.0.0.1",
             "--port", str(self.daemon.port), "--user", "alice", "--session", self.session],
            capture_output=True, text=True, env=self.env, cwd=str(ROOT),
        )
        self.assertEqual(r.returncode, 0, r.stderr)

    def tearDown(self):
        subprocess.run([sys.executable, "-m", "pairshell", "stop", "tel"], capture_output=True, env=self.env, cwd=str(ROOT))
        subprocess.run(["tmux", "kill-session", "-t", f"={self.session}:"], capture_output=True)
        self.daemon.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_attach_and_detach(self):
        import pty

        pid, fd = pty.fork()
        if pid == 0:  # child
            os.chdir(str(ROOT))
            os.execvpe(sys.executable, [sys.executable, "-m", "pairshell", "attach", "tel"], self.env)
        self._fcntl.ioctl(fd, self._termios.TIOCSWINSZ, self._struct.pack("HHHH", 30, 100, 0, 0))
        try:
            out = read_until(fd, b"[pairshell] attached to tel", 90)
            self.assertIn(b"example-host login:", out)  # banner echoed to the user
            self.assertNotIn(b"pw", out.split(b"Password:")[-1][:20])  # password never echoed
            # tmux status line "[psatt_1234:tcsh*" (the name is truncated to
            # 10 chars; the echoed `tmux new -A -s` line never has the bracket)
            read_until(fd, b"[" + self.session[:6].encode(), 30)
            # the tmux client told the server our size: 100x30 via NAWS
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and (100, 30) not in self.daemon.naws:
                time.sleep(0.2)
            self.assertIn((100, 30), self.daemon.naws)
            # type into the shared shell through the attach client
            os.write(fd, b"echo via-attach\r")
            read_until(fd, b"via-attach", 15)
            # Ctrl-] disconnects; the tmux session survives
            os.write(fd, b"\x1d")
            out = read_until(fd, b"disconnected from tel", 20)
            self.assertIn(b"still running", out)
            _, status = os.waitpid(pid, 0)
            self.assertEqual(os.waitstatus_to_exitcode(status), 0)
            r = subprocess.run(["tmux", "has-session", "-t", f"={self.session}:"], capture_output=True)
            self.assertEqual(r.returncode, 0, "tmux session should survive detaching")
            # and the agent side still works against the same session
            r = subprocess.run(
                [sys.executable, "-m", "pairshell", "screen", "--to", "tel", "-n", "50"],
                capture_output=True, text=True, env=self.env, cwd=str(ROOT), timeout=90,
            )
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("via-attach", r.stdout)
        finally:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
            os.close(fd)


if __name__ == "__main__":
    unittest.main()
