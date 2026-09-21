"""Integration tests against a real tmux, a fake telnetd and a local sshd.

Everything here is skipped on Windows or when the needed binaries are
missing, so the unit tests stay runnable anywhere.
"""

from __future__ import annotations

import getpass
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

POSIX = sys.platform != "win32"
HAVE_TMUX = POSIX and bool(shutil.which("tmux")) and bool(shutil.which("bash"))
HAVE_TCSH = bool(shutil.which("tcsh"))
HAVE_SSHD = POSIX and bool(shutil.which("sshd")) and bool(shutil.which("ssh")) and bool(shutil.which("ssh-keygen"))

ROOT = Path(__file__).resolve().parent.parent


def unique(prefix: str) -> str:
    return f"{prefix}_{os.getpid()}_{int(time.time() * 1000) % 100000}"


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def wait_idle(session, timeout: float = 8.0) -> None:
    """Wait for the pane shell to show its first prompt."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if session.status()["idle"]:
            return
        time.sleep(0.2)
    raise AssertionError("pane never became idle: %r" % (session.status(),))


# --------------------------------------------------------------------------
# TmuxSession over the local transport
# --------------------------------------------------------------------------


@unittest.skipUnless(HAVE_TMUX, "needs tmux and bash (POSIX)")
class LocalTmuxTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="pairshell-it-")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        from pairshell.tmuxops import TmuxSession
        from pairshell.transports.local import LocalTransport

        self.t = LocalTransport()
        self.t.connect()
        self.name = unique("pstest")
        self.s = TmuxSession(self.t, self.name, log_dir=self.tmp)
        self.assertTrue(self.s.ensure())
        self.assertFalse(self.s.ensure())
        wait_idle(self.s)

    def tearDown(self):
        self.t.run(f"tmux kill-session -t ={self.name}: 2>/dev/null")
        self.t.close()

    def test_exec_basics(self):
        r = self.s.exec("pwd")
        self.assertEqual((r["status"], r["rc"]), ("done", 0))
        self.assertEqual(len(r["output"]), 1)
        self.assertTrue(os.path.isdir(r["output"][0]))
        self.assertEqual(self.s.exec("ls /")["rc"], 0)
        self.assertEqual(self.s.exec("false")["rc"], 1)
        self.assertEqual(self.s.exec("(exit 7)")["rc"], 7)
        r = self.s.exec("printf abc")
        self.assertEqual((r["rc"], r["output"]), (0, ["abc"]))
        r = self.s.exec("""echo "a;b" 'c d' $((1+2)) \\$HOME""")
        self.assertEqual(r["output"], ["a;b c d 3 $HOME"])

    def test_state_persists_between_calls(self):
        self.assertEqual(self.s.exec("cd /tmp")["rc"], 0)
        self.assertEqual(self.s.exec("pwd")["output"], ["/tmp"])
        self.assertEqual(self.s.exec("export PS_TEST_VAR=42")["rc"], 0)
        self.assertEqual(self.s.exec("echo $PS_TEST_VAR")["output"], ["42"])

    def test_timeout_busy_and_interrupt(self):
        t0 = time.monotonic()
        r = self.s.exec("sleep 30", timeout=1)
        self.assertEqual((r["status"], r["rc"]), ("timeout", 124))
        self.assertLess(time.monotonic() - t0, 5)
        r = self.s.exec("echo hi", timeout=2)
        self.assertEqual((r["status"], r["rc"]), ("busy", 3))
        self.assertIn("sleep", r["reason"])
        scr = self.s.keys([("key", "C-c")])
        self.assertTrue(scr["idle"], scr)
        r = self.s.exec("echo after")
        self.assertEqual((r["rc"], r["output"]), (0, ["after"]))

    def test_busy_while_user_is_typing(self):
        self.s.send_keys([("literal", "echo typed but not entered")])
        r = self.s.exec("echo hi")
        self.assertEqual(r["rc"], 3)
        self.assertIn("typing", r["reason"])
        r = self.s.exec("echo forced", force=True)
        self.assertEqual(r["status"], "done")
        self.assertIn("forced", r["output"][-1])
        self.assertEqual(self.s.status()["idle"], True)

    def test_truncation(self):
        r = self.s.exec("seq 1 700", max_lines=100)
        self.assertEqual((r["rc"], r["omitted"], len(r["output"])), (0, 600, 100))
        self.assertEqual((r["output"][0], r["output"][-1]), ("601", "700"))

    def test_background_job(self):
        r = self.s.exec("sleep 2 &")
        self.assertEqual((r["status"], r["rc"]), ("done", 0))

    def test_no_sentinel_subshell(self):
        t0 = time.monotonic()
        r = self.s.exec("bash --norc --noprofile", timeout=20)
        self.assertEqual((r["status"], r["rc"]), ("no_sentinel", 125))
        self.assertLess(time.monotonic() - t0, 6)
        self.assertEqual(self.s.exec("echo inner")["output"], ["inner"])
        r = self.s.exec("exit", timeout=20)
        self.assertEqual(r["status"], "no_sentinel")
        self.assertEqual(self.s.exec("echo back")["output"], ["back"])

    def test_keys_literal_and_screen(self):
        scr = self.s.keys([("literal", "echo semi;"), ("key", "Enter")])
        self.assertIn("semi", scr["lines"][-2])
        self.assertTrue(scr["idle"])
        scr = self.s.screen(5)
        self.assertIn("foreground", scr)
        self.assertTrue(any("semi" in ln for ln in scr["lines"]))

    def test_transcript_log(self):
        # ensure() ran twice in setUp: the pipe must survive repeated calls
        self.s.ensure()
        self.s.exec("echo transcript-marker")
        time.sleep(0.5)
        log = Path(self.tmp) / f"{self.name}.log"
        self.assertTrue(log.exists())
        self.assertIn("transcript-marker", log.read_text(errors="replace"))
        self.assertEqual(self.t.run(f"tmux display -p -t ={self.name}: '#{{pane_pipe}}'")[1].strip(), "1")

    def test_status_fields(self):
        st = self.s.status()
        self.assertEqual(st["session"], self.name)
        self.assertEqual(st["shell_family"], "sh")
        self.assertEqual(st["window"], "0.0")
        self.assertEqual(st["size"], "200x50")
        self.assertEqual(st["attached_clients"], 0)
        self.assertTrue(st["idle"])

    @unittest.skipUnless(HAVE_TCSH, "needs tcsh")
    def test_tcsh_pane(self):
        from pairshell.tmuxops import TmuxSession

        name = unique("pstcsh")
        rc, out = self.t.run(f"tmux new-session -d -s {name} -x 200 -y 50 tcsh")
        self.assertEqual(rc, 0, out)
        try:
            s = TmuxSession(self.t, name, log_dir=self.tmp)
            wait_idle(s)
            st = s.status()
            self.assertIn(st["foreground"], ("tcsh", "csh"))  # Ubuntu reports tcsh as csh
            self.assertEqual(st["shell_family"], "csh")
            self.assertEqual(s.exec("ls >& /dev/null")["rc"], 0)
            self.assertEqual(s.exec("false")["rc"], 1)
            self.assertEqual(s.exec("(exit 5)")["rc"], 5)
            r = s.exec("echo $shell")
            self.assertIn("tcsh", r["output"][0])
            r = s.exec("printf abc")
            self.assertEqual(r["output"], ["abc"])
            # a csh parse error rejects the whole line: no sentinel, quick return
            r = s.exec("ls 2>/dev/null >/dev/null", timeout=20)
            self.assertEqual((r["status"], r["rc"]), ("no_sentinel", 125))
        finally:
            self.t.run(f"tmux kill-session -t ={name}: 2>/dev/null")


# --------------------------------------------------------------------------
# The real CLI + a background serve process
# --------------------------------------------------------------------------


@unittest.skipUnless(HAVE_TMUX, "needs tmux and bash (POSIX)")
class CliServeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pairshell-cli-")
        self.env = dict(os.environ, PAIRSHELL_HOME=self.tmp, HOME=self.tmp)
        self.session = unique("pscli")

    def tearDown(self):
        self.run_cli("stop", "p1")
        subprocess.run(["tmux", "kill-session", "-t", f"={self.session}:"], capture_output=True, env=self.env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_cli(self, *args, timeout=90):
        return subprocess.run(
            [sys.executable, "-m", "pairshell", *args],
            capture_output=True,
            text=True,
            env=self.env,
            cwd=str(ROOT),
            timeout=timeout,
        )

    def test_full_flow(self):
        r = self.run_cli("add", "p1", "--protocol", "local", "--session", self.session)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.run_cli("status", "--json")
        self.assertFalse(json.loads(r.stdout)["serve"]["running"])

        r = self.run_cli("exec", "pwd", "ls /etc/hostname")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("starting serve", r.stderr)
        self.assertIn("### pwd", r.stdout)
        self.assertIn("### rc=0", r.stdout)
        self.assertIn("/etc/hostname", r.stdout)

        self.assertEqual(self.run_cli("exec", "false").returncode, 1)
        r = self.run_cli("exec", "printf abc")
        self.assertEqual((r.returncode, r.stdout), (0, "abc\n"))

        r = self.run_cli("exec", "sleep 30", "--timeout", "1")
        self.assertEqual(r.returncode, 124)
        self.assertIn("rc 124", r.stderr)
        r = self.run_cli("exec", "echo hi")
        self.assertEqual(r.returncode, 3)
        self.assertIn("busy", r.stderr)
        r = self.run_cli("exec", "echo one", "sleep 30", "echo never", "--timeout", "1")
        self.assertEqual(r.returncode, 3)  # first is busy: stop right there
        r = self.run_cli("keys", "C-c")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("idle", r.stderr)
        r = self.run_cli("exec", "echo one", "false", "echo three")
        self.assertEqual(r.returncode, 1)  # a plain failure does not stop the batch
        self.assertIn("### rc=1", r.stdout)
        self.assertIn("three", r.stdout)

        r = self.run_cli("status", "--json")
        info = json.loads(r.stdout)
        self.assertTrue(info["serve"]["running"])
        self.assertTrue(info["connected"])
        self.assertTrue(info["idle"])
        r = self.run_cli("list", "--json")
        rows = json.loads(r.stdout)
        self.assertEqual((rows[0]["name"], rows[0]["state"], rows[0]["current"]), ("p1", "idle", False))

        r = self.run_cli("screen", "-n", "10")
        self.assertIn("printf abc", r.stdout)
        r = self.run_cli("ctl", "tmux display -p -t =%s: '#{session_name}'" % self.session)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, self.session))

        # kill serve the hard way; the next call restarts it and the shell state survives
        self.run_cli("exec", "cd /tmp")
        state = json.loads((Path(self.tmp) / "run" / "p1.json").read_text())
        os.kill(state["pid"], 9)
        time.sleep(0.3)
        r = self.run_cli("exec", "pwd")
        self.assertEqual((r.returncode, r.stdout), (0, "/tmp\n"), r.stderr)
        self.assertIn("starting serve", r.stderr)

        r = self.run_cli("stop", "p1")
        self.assertIn("stopped", r.stderr)
        self.assertFalse(json.loads(self.run_cli("status", "--json").stdout)["serve"]["running"])
        r = self.run_cli("rm", "p1", "-y")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(self.run_cli("list", "--json").stdout), [])

    def test_two_profiles_and_current(self):
        s2 = self.session + "b"
        try:
            self.run_cli("add", "p1", "--protocol", "local", "--session", self.session)
            self.run_cli("add", "p2", "--protocol", "local", "--session", s2)
            r = self.run_cli("exec", "pwd")
            self.assertEqual(r.returncode, 2)
            self.assertIn("no current profile", r.stderr)
            self.assertEqual(self.run_cli("exec", "--to", "p1", "cd /").returncode, 0)
            self.assertEqual(self.run_cli("exec", "--to", "p2", "cd /tmp").returncode, 0)
            self.assertEqual(self.run_cli("exec", "--to", "p1", "pwd").stdout, "/\n")
            self.assertEqual(self.run_cli("exec", "--to", "p2", "pwd").stdout, "/tmp\n")
            self.assertEqual(self.run_cli("current", "p2").returncode, 0)
            self.assertEqual(self.run_cli("exec", "pwd").stdout, "/tmp\n")
            rows = {r["name"]: r for r in json.loads(self.run_cli("list", "--json").stdout)}
            self.assertEqual((rows["p1"]["state"], rows["p2"]["state"], rows["p2"]["current"]), ("idle", "idle", True))
            self.assertNotEqual(rows["p1"]["rpc_port"], rows["p2"]["rpc_port"])
        finally:
            self.run_cli("stop", "p2")
            subprocess.run(["tmux", "kill-session", "-t", f"={s2}:"], capture_output=True, env=self.env)


# --------------------------------------------------------------------------
# Telnet transport against the fake telnetd
# --------------------------------------------------------------------------


@unittest.skipUnless(POSIX, "fake telnetd needs pty (POSIX)")
class TelnetTransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests.fake_telnetd import FakeTelnetd

        cls.home = tempfile.mkdtemp(prefix="pairshell-telnet-")
        shell = [shutil.which("tcsh")] if HAVE_TCSH else [shutil.which("bash"), "--norc", "--noprofile", "-i"]
        cls.daemon = FakeTelnetd(user="alice", password="s3cret", shell=shell, home=cls.home)

    @classmethod
    def tearDownClass(cls):
        cls.daemon.close()
        shutil.rmtree(cls.home, ignore_errors=True)

    def test_login_run_and_reconnect(self):
        from pairshell.transports.telnet import TelnetTransport

        t = TelnetTransport("127.0.0.1", self.daemon.port, "alice", "s3cret")
        t.connect()
        try:
            self.assertTrue(t.connected)
            self.assertEqual(t.run("echo hi"), (0, "hi\n"))
            self.assertEqual(t.run("(exit 4)")[0], 4)
            self.assertEqual(t.run("printf abc"), (0, "abc"))
            rc, out = t.run("echo $0; echo LANG=$LANG; stty -a | head -c 0; tty >/dev/null && echo tty")
            self.assertIn("bash", out)
            self.assertIn("tty", out)
            self.assertIn(b"dumb", self.daemon.term_types)
            self.assertIn(("alice", "s3cret"), self.daemon.logins)
            # a dropped connection is re-established on the next call
            t.close()
            self.assertFalse(t.connected)
            self.assertEqual(t.run("echo again"), (0, "again\n"))
            self.assertEqual(t.connect_count, 2)
        finally:
            t.close()

    def test_bad_password(self):
        from pairshell.transports.base import AuthError
        from pairshell.transports.telnet import TelnetTransport

        t = TelnetTransport("127.0.0.1", self.daemon.port, "alice", "wrong")
        with self.assertRaises(AuthError):
            t.connect()
        self.assertFalse(t.connected)

    def test_connection_refused(self):
        from pairshell.transports.base import TransportError
        from pairshell.transports.telnet import TelnetTransport

        t = TelnetTransport("127.0.0.1", free_port(), "alice", "x")
        with self.assertRaises(TransportError):
            t.connect()

    @unittest.skipUnless(HAVE_TMUX, "needs tmux")
    def test_tmux_over_telnet(self):
        from pairshell.tmuxops import TmuxSession
        from pairshell.transports.telnet import TelnetTransport

        t = TelnetTransport("127.0.0.1", self.daemon.port, "alice", "s3cret")
        t.connect()
        name = unique("pstel")
        try:
            s = TmuxSession(t, name, log_dir=self.home)
            self.assertTrue(s.ensure())
            wait_idle(s)
            r = s.exec("echo over-telnet && (exit 3)")
            self.assertEqual((r["rc"], r["output"]), (3, ["over-telnet"]))
            self.assertEqual(s.exec("printf abc")["output"], ["abc"])
        finally:
            t.run(f"tmux kill-session -t ={name}: 2>/dev/null")
            t.close()


# --------------------------------------------------------------------------
# SSH transport against a throwaway sshd
# --------------------------------------------------------------------------


@unittest.skipUnless(HAVE_SSHD and HAVE_TMUX, "needs sshd, ssh, ssh-keygen and tmux")
class SshTransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="pairshell-ssh-")
        d = Path(cls.tmp)
        os.chmod(cls.tmp, 0o700)
        for name in ("hostkey", "userkey"):
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(d / name)], check=True, capture_output=True)
        (d / "authorized_keys").write_text((d / "userkey.pub").read_text())
        cls.port = free_port()
        cls.user = getpass.getuser()
        config = "\n".join(
            [
                f"Port {cls.port}",
                "ListenAddress 127.0.0.1",
                f"HostKey {d / 'hostkey'}",
                f"AuthorizedKeysFile {d / 'authorized_keys'}",
                "PasswordAuthentication no",
                "KbdInteractiveAuthentication no",
                "PubkeyAuthentication yes",
                "StrictModes no",
                "UsePAM no",
                "PermitRootLogin yes",
                "PidFile none",
                "LogLevel ERROR",
                "PermitUserEnvironment no",
                "",
            ]
        )
        (d / "sshd_config").write_text(config)
        cls.sshd = subprocess.Popen(
            [shutil.which("sshd"), "-D", "-e", "-f", str(d / "sshd_config")],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        cls.ssh_options = ["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR"]
        deadline = time.monotonic() + 10
        ready = False
        while time.monotonic() < deadline and cls.sshd.poll() is None:
            try:
                socket.create_connection(("127.0.0.1", cls.port), timeout=0.5).close()
                ready = True
                break
            except OSError:
                time.sleep(0.2)
        if not ready:
            cls.sshd.kill()
            raise unittest.SkipTest("sshd did not start: %r" % (cls.sshd.stdout.read()[-400:] if cls.sshd.stdout else b""))
        probe = subprocess.run(
            ["ssh", "-T", "-o", "BatchMode=yes", *cls.ssh_options, "-p", str(cls.port), "-i", str(d / "userkey"), f"{cls.user}@127.0.0.1", "true"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if probe.returncode != 0:
            cls.sshd.kill()
            raise unittest.SkipTest(f"cannot ssh into the test sshd as {cls.user}: {probe.stderr.strip()[-300:]}")

    @classmethod
    def tearDownClass(cls):
        cls.sshd.kill()
        cls.sshd.wait()
        if cls.sshd.stdout:
            cls.sshd.stdout.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _transport(self, key="userkey"):
        from pairshell.transports.ssh import SshTransport

        return SshTransport("127.0.0.1", self.port, self.user, key_path=str(Path(self.tmp) / key), ssh_options=self.ssh_options)

    def test_run(self):
        t = self._transport()
        t.connect()
        try:
            self.assertEqual(t.run("echo hi"), (0, "hi\n"))
            self.assertEqual(t.run("(exit 9)")[0], 9)
            self.assertEqual(t.run("printf abc"), (0, "abc"))
            self.assertEqual(t.run("cat"), (0, ""))  # stdin is /dev/null
        finally:
            t.close()

    def test_auth_failure(self):
        from pairshell.transports.base import TransportError

        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(Path(self.tmp) / "badkey")], check=True, capture_output=True)
        t = self._transport("badkey")
        with self.assertRaises(TransportError) as cm:
            t.connect()
        self.assertIn("denied", str(cm.exception).lower())

    def test_tmux_over_ssh(self):
        from pairshell.tmuxops import TmuxSession

        t = self._transport()
        t.connect()
        name = unique("psssh")
        try:
            s = TmuxSession(t, name, log_dir=self.tmp)
            s.ensure()
            wait_idle(s)
            r = s.exec("echo over-ssh")
            self.assertEqual((r["rc"], r["output"]), (0, ["over-ssh"]))
            r = s.exec("sleep 30", timeout=1)
            self.assertEqual(r["rc"], 124)
            self.assertTrue(s.keys([("key", "C-c")])["idle"])
        finally:
            t.run(f"tmux kill-session -t ={name}: 2>/dev/null")
            t.close()


if __name__ == "__main__":
    unittest.main()
