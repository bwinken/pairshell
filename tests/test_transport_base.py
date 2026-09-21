"""Control-channel protocol tests with a scripted in-process 'remote'."""

import os
import re
import threading
import time
import unittest

from pairshell.transports.base import (
    ByteStream,
    ConnectionLost,
    ControlTimeout,
    StreamTransport,
    ctl_sentinel_pattern,
    wrap_control_command,
)

PRINTF_RE = re.compile(rb"printf '\\n__CTL_%s_([0-9a-f]+)__\\n' \"\$\?\"\n")


class ScriptedTransport(StreamTransport):
    """A StreamTransport whose remote is a thread answering from a script.

    ``responder(cmd) -> (rc, output_bytes)``; return ``None`` to simulate the
    connection dropping while the command runs.
    """

    name = "scripted"

    def __init__(self, responder):
        super().__init__()
        self.responder = responder
        self.opened = 0

    def _open(self):
        self.opened += 1
        r_in, w_in = os.pipe()
        r_out, w_out = os.pipe()

        def remote():
            buf = b""
            try:
                while True:
                    chunk = os.read(r_in, 65536)
                    if not chunk:
                        break
                    buf += chunk
                    while True:
                        m = PRINTF_RE.search(buf)
                        if not m:
                            break
                        block, buf = buf[: m.start()], buf[m.end() :]
                        # the setup line arrives before the first block
                        cmd = block.rsplit(b"( ", 1)[-1].split(b"\n) </dev/null", 1)[0]
                        resp = self.responder(cmd.decode())
                        if resp is None:
                            os.close(w_out)
                            return
                        rc, out = resp
                        os.write(w_out, out + b"\n__CTL_%d_%s__\n" % (rc, m.group(1)))
            except OSError:
                pass
            finally:
                try:
                    os.close(w_out)
                except OSError:
                    pass

        threading.Thread(target=remote, daemon=True).start()

        def close():
            for fd in (w_in, r_out):
                try:
                    os.close(fd)
                except OSError:
                    pass

        return ByteStream(lambda: os.read(r_out, 65536), lambda d: os.write(w_in, d), close, name="scripted").start()


class WrapTests(unittest.TestCase):
    def test_wrap_control_command(self):
        w = wrap_control_command("echo hi", "abcd1234")
        self.assertEqual(w, b"( echo hi\n) </dev/null 2>&1\nprintf '\\n__CTL_%s_abcd1234__\\n' \"$?\"\n")
        m = ctl_sentinel_pattern("abcd1234").search(b"junk\n__CTL_7_abcd1234__\n")
        self.assertEqual(m.group(1), b"7")
        self.assertIsNone(ctl_sentinel_pattern("abcd1234").search(b"__CTL_7_ffffffff__"))


class ByteStreamTests(unittest.TestCase):
    def _pipe_stream(self):
        r, w = os.pipe()
        s = ByteStream(lambda: os.read(r, 65536), lambda d: os.write(w, d), lambda: (os.close(r), os.close(w)), name="t").start()
        return s, w

    def test_read_until_across_chunks_and_eof(self):
        s, w = self._pipe_stream()
        os.write(w, b"abc__CTL_")
        threading.Timer(0.05, lambda: os.write(w, b"0_abcd1234__\nrest")).start()
        before, m = s.read_until(ctl_sentinel_pattern("abcd1234"), 2.0)
        self.assertEqual((before, m.group(1)), (b"abc", b"0"))
        self.assertEqual(s.read_any(0.5), b"\nrest")
        with self.assertRaises(ControlTimeout):
            s.read_until(ctl_sentinel_pattern("abcd1234"), 0.1)
        os.close(w)
        with self.assertRaises(ConnectionLost):
            s.read_until(ctl_sentinel_pattern("abcd1234"), 1.0)

    def test_wait_quiet(self):
        s, w = self._pipe_stream()
        os.write(w, b"Last login: today\n")
        data, matched = s.wait_quiet(0.2, 2.0, stop_pattern=re.compile(rb"login incorrect"))
        self.assertEqual((data, matched), (b"Last login: today\n", False))
        os.write(w, b"Login incorrect\n")
        data, matched = s.wait_quiet(0.2, 2.0, stop_pattern=re.compile(rb"(?i)login incorrect"))
        self.assertTrue(matched)
        t0 = time.monotonic()
        data, matched = s.wait_quiet(0.2, 0.3)
        self.assertLess(time.monotonic() - t0, 1.0)
        self.assertEqual(data, b"")
        s.close()


class StreamTransportTests(unittest.TestCase):
    def test_run_and_exit_codes(self):
        t = ScriptedTransport(lambda cmd: (3, b"out\r\nline2") if cmd == "x" else (0, b""))
        t.connect()
        self.assertTrue(t.connected)
        self.assertEqual(t.run("x"), (3, "out\nline2"))
        self.assertEqual(t.run("y"), (0, ""))
        self.assertEqual(t.connect_count, 1)
        t.close()
        self.assertFalse(t.connected)

    def test_timeout_drops_connection_then_relogins(self):
        def responder(cmd):
            if cmd == "slow":
                time.sleep(0.6)
            return (0, b"ok")

        t = ScriptedTransport(responder)
        t.connect()
        with self.assertRaises(ControlTimeout):
            t.run("slow", timeout=0.2)
        self.assertFalse(t.connected)
        self.assertEqual(t.run("fast"), (0, "ok"))
        self.assertEqual(t.opened, 2)

    def test_eof_triggers_one_relogin_and_retry(self):
        calls = []

        def responder(cmd):
            calls.append(cmd)
            if cmd == "flaky" and calls.count("flaky") == 1:
                return None  # connection drops
            return (0, b"done")

        t = ScriptedTransport(responder)
        t.connect()
        self.assertEqual(t.run("flaky"), (0, "done"))
        self.assertEqual(t.opened, 2)
        self.assertEqual(calls.count("flaky"), 2)

    def test_eof_twice_raises(self):
        t = ScriptedTransport(lambda cmd: None if cmd == "dead" else (0, b""))
        t.connect()
        with self.assertRaises(ConnectionLost):
            t.run("dead")
        self.assertFalse(t.connected)

    def test_nul_rejected(self):
        t = ScriptedTransport(lambda cmd: (0, b""))
        with self.assertRaises(Exception):
            t.run("a\0b")


if __name__ == "__main__":
    unittest.main()
