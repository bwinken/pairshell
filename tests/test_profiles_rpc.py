"""Profile store, current marker, run-state and the local RPC."""

import os
import tempfile
import threading
import unittest
from pathlib import Path

from pairshell import rpc
from pairshell.profiles import (
    Profile,
    ProfileError,
    ProfileNotFound,
    ProfileStore,
    RunState,
    clear_current,
    config_dir,
    get_current,
    read_run_state,
    sanitize_session_name,
    serve_state,
    set_current,
    write_run_state,
)


class TempHome(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old = os.environ.get("PAIRSHELL_HOME")
        os.environ["PAIRSHELL_HOME"] = self._tmp.name

    def tearDown(self):
        if self._old is None:
            os.environ.pop("PAIRSHELL_HOME", None)
        else:
            os.environ["PAIRSHELL_HOME"] = self._old
        self._tmp.cleanup()


class ProfileStoreTests(TempHome):
    def test_paths_follow_env(self):
        self.assertEqual(config_dir(), Path(self._tmp.name))

    def test_round_trip_and_ports(self):
        store = ProfileStore()
        a = store.upsert(Profile(name="lab.1", protocol="telnet", host="example-host", user="alice"))
        b = store.upsert(Profile(name="b2", protocol="ssh", host="192.0.2.10", user="bob", key_path="~/.ssh/id_ed25519"))
        self.assertEqual((a.port, a.session, a.rpc_port), (23, "lab_1", 47100))
        self.assertEqual((b.port, b.session, b.rpc_port), (22, "b2", 47101))
        again = store.get("lab.1")
        self.assertEqual(again.to_dict(), a.to_dict())
        # editing keeps the port; a clash gets a fresh one
        a.host = "other-host"
        self.assertEqual(store.upsert(a).rpc_port, 47100)
        c = Profile(name="c3", protocol="ssh", host="h", user="u", rpc_port=47100)
        self.assertEqual(store.upsert(c).rpc_port, 47102)
        self.assertEqual(store.names(), ["b2", "c3", "lab.1"])
        store.touch("b2")
        self.assertEqual([p.name for p in store.list()][0], "b2")
        store.remove("b2")
        with self.assertRaises(ProfileNotFound):
            store.get("b2")
        with self.assertRaises(ProfileNotFound):
            store.remove("b2")

    def test_validation(self):
        with self.assertRaises(ProfileError):
            Profile(name="bad name", protocol="ssh", host="h", user="u").validate()
        with self.assertRaises(ProfileError):
            Profile(name="x", protocol="ftp", host="h", user="u").validate()
        with self.assertRaises(ProfileError):
            Profile(name="x", protocol="ssh", host="", user="u").validate()
        with self.assertRaises(ProfileError):
            Profile(name="x", protocol="ssh", host="h", user="u", session="a:b").validate()
        Profile(name="x", protocol="local").validate()

    def test_sanitize_session_name(self):
        self.assertEqual(sanitize_session_name("lab.1"), "lab_1")
        self.assertEqual(sanitize_session_name("-x"), "s_-x")
        self.assertEqual(sanitize_session_name("ok-name_1"), "ok-name_1")

    def test_from_dict_tolerates_unknown_and_missing(self):
        p = Profile.from_dict({"name": "n", "protocol": "ssh", "host": "h", "user": "u", "port": "2222", "weird": 1})
        self.assertEqual((p.port, p.ssh_options, p.session), (2222, [], "n"))

    def test_current(self):
        self.assertIsNone(get_current())
        set_current("lab1")
        self.assertEqual(get_current(), "lab1")
        clear_current()
        self.assertIsNone(get_current())
        store = ProfileStore()
        store.upsert(Profile(name="z", protocol="local"))
        set_current("z")
        store.remove("z")
        self.assertIsNone(get_current())

    def test_run_state_and_stale_pid(self):
        st = RunState(pid=os.getpid(), rpc_port=47100, token="t", started_at=1.0, profile="p", transport="local")
        write_run_state(st)
        self.assertEqual(read_run_state("p"), st)
        self.assertTrue(serve_state("p")["running"])
        dead = RunState(pid=2**22 + 12345, rpc_port=1, token="t", started_at=1.0, profile="dead")
        write_run_state(dead)
        s = serve_state("dead")
        self.assertFalse(s["running"])
        self.assertTrue(s["stale"])
        self.assertIsNone(read_run_state("dead"))  # stale file removed


class RpcTests(unittest.TestCase):
    def setUp(self):
        def handler(op, args):
            if op == "ping":
                return {"pong": True, "args": args}
            if op == "boom":
                raise ValueError("kaboom")
            raise KeyError(op)

        self.server = rpc.RpcServer(0, "tok", handler)
        self.server.start()

    def tearDown(self):
        self.server.stop()

    def test_round_trip(self):
        res = rpc.call(self.server.port, "tok", "ping", {"x": "ü"})
        self.assertEqual(res, {"pong": True, "args": {"x": "ü"}})

    def test_bad_token(self):
        with self.assertRaises(rpc.RpcRemoteError) as cm:
            rpc.call(self.server.port, "nope", "ping")
        self.assertEqual(cm.exception.kind, "Unauthorized")

    def test_handler_error(self):
        with self.assertRaises(rpc.RpcRemoteError) as cm:
            rpc.call(self.server.port, "tok", "boom")
        self.assertEqual((cm.exception.kind, str(cm.exception)), ("ValueError", "kaboom"))

    def test_unreachable(self):
        self.server.stop()
        with self.assertRaises(rpc.RpcError):
            rpc.call(self.server.port, "tok", "ping", connect_timeout=0.5)

    def test_concurrent_calls(self):
        results = []

        def worker(i):
            results.append(rpc.call(self.server.port, "tok", "ping", {"i": i})["args"]["i"])

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(results), list(range(8)))


if __name__ == "__main__":
    unittest.main()
