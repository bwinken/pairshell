"""``pairshell serve <profile>``: hold the control channel, answer RPC.

One serve process per profile.  It owns the single persistent Telnet/SSH
connection, keeps it alive with a ``:`` every 240 s, and exposes the tmux
operations over the local JSON-line RPC.  It is normally started in the
background by ``attach``, the menu or any client command that needs it.
"""

from __future__ import annotations

import logging
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from typing import Any, Callable

from . import rpc
from .credentials import resolve_password
from .profiles import (
    RPC_PORT_RANGE,
    Profile,
    ProfileStore,
    RunState,
    pid_alive,
    read_run_state,
    remove_run_state,
    run_dir,
    run_state_is_ours,
    serve_log_path,
    serve_state,
    serve_stderr_path,
    write_run_state,
)
from .tmuxops import TmuxSession
from .transports import AuthError, TransportError, make_transport

log = logging.getLogger("pairshell.serve")

#: Captured at import, i.e. right after the serve process started; the
#: run-state records it so a reused pid can be told apart from our serve.
PROCESS_START = time.time()

KEEPALIVE_INTERVAL = 240.0
START_WAIT = 60.0
PING_TIMEOUT = 2.0


class ServeError(Exception):
    pass


class Server:
    """The long-running object behind ``pairshell serve``."""

    def __init__(self, profile: Profile, password: str | None) -> None:
        self.profile = profile
        self.transport = make_transport(profile, password)
        self.tmux = TmuxSession(
            self.transport,
            profile.session,
            prompt_regex=profile.prompt_regex or None,
            transcript_max_bytes=int(profile.transcript_mb) * 1024 * 1024,
        )
        self.token = secrets.token_hex(16)
        self.started_at = PROCESS_START
        self._stop = threading.Event()
        self.rpc: rpc.RpcServer | None = None

    # -- RPC dispatch ------------------------------------------------------------

    def handle(self, op: str, args: dict[str, Any]) -> Any:
        if op == "ping":
            return self._info()
        if op == "shutdown":
            threading.Timer(0.2, self.request_stop).start()
            return {"stopping": True}
        if op == "status":
            info = self._info()
            try:
                self.tmux.ensure()
                info.update(self.tmux.status())
                info["ok"] = True
            except TransportError as exc:
                info["ok"] = False
                info["error"] = str(exc)
            info["connected"] = self.transport.connected
            return info
        if op == "ensure":
            return {"created": self.tmux.ensure()}
        if op == "exec":
            self.tmux.ensure()
            return self.tmux.exec(
                str(args.get("cmd", "")),
                timeout=float(args.get("timeout", 120)),
                force=bool(args.get("force", False)),
                max_lines=int(args.get("max_lines", 500)),
            )
        if op == "wait":
            self.tmux.ensure()
            return self.tmux.wait(timeout=float(args.get("timeout", 120)), max_lines=int(args.get("max_lines", 500)))
        if op == "screen":
            self.tmux.ensure()
            return self.tmux.screen(int(args.get("lines", 0)))
        if op == "keys":
            self.tmux.ensure()
            items = [(str(k), str(v)) for k, v in args.get("items", [])]
            return self.tmux.keys(items)
        if op == "ctl":
            return self.tmux.ctl(str(args.get("cmd", "")), timeout=float(args.get("timeout", 30)))
        raise ValueError(f"unknown op {op!r}")

    def _info(self) -> dict[str, Any]:
        return {
            "profile": self.profile.name,
            "session": self.profile.session,
            "pid": os.getpid(),
            "rpc_port": self.rpc.port if self.rpc else None,
            "started_at": self.started_at,
            "uptime": time.time() - self.started_at,
            "transport": self.transport.describe(),
            "connected": self.transport.connected,
            "connect_count": getattr(self.transport, "connect_count", None),
            "last_connect_error": getattr(self.transport, "last_connect_error", None),
        }

    # -- lifecycle ---------------------------------------------------------------

    def request_stop(self) -> None:
        self._stop.set()

    def _keepalive(self) -> None:
        while not self._stop.wait(5.0):
            if not self.transport.connected:
                continue
            if time.monotonic() - self.tmux.last_activity < KEEPALIVE_INTERVAL:
                continue
            try:
                self.tmux.last_activity = time.monotonic()
                self.transport.run(":", timeout=20.0)
            except TransportError as exc:
                log.warning("keepalive failed: %s (will re-login on the next call)", exc)

    def run(self) -> int:
        name = self.profile.name
        existing = read_run_state(name)
        if existing is not None and existing.pid != os.getpid() and pid_alive(existing.pid) and ping(existing) is not None:
            log.error("serve for %s is already running (pid %s)", name, existing.pid)
            return 2
        log.info("connecting: %s", self.transport.describe())
        self.transport.connect()
        created = self.tmux.ensure()
        log.info("tmux session %s %s", self.profile.session, "created" if created else "exists")
        self.rpc = rpc.bind_server(self.profile.rpc_port, self.token, self.handle, list(RPC_PORT_RANGE))
        if self.rpc.port != self.profile.rpc_port:
            log.warning("rpc port %s busy, using %s", self.profile.rpc_port, self.rpc.port)
            ProfileStore().set_rpc_port(name, self.rpc.port)
        write_run_state(
            RunState(
                pid=os.getpid(),
                rpc_port=self.rpc.port,
                token=self.token,
                started_at=self.started_at,
                profile=name,
                transport=self.profile.protocol,
            )
        )
        self.rpc.start()
        ProfileStore().touch(name)
        threading.Thread(target=self._keepalive, name="keepalive", daemon=True).start()
        log.info("serving %s on 127.0.0.1:%d (pid %d)", name, self.rpc.port, os.getpid())
        try:
            while not self._stop.wait(0.5):
                pass
        finally:
            log.info("stopping")
            try:
                self.rpc.stop()
            except Exception:  # pragma: no cover - best effort
                pass
            self.transport.close()
            st = read_run_state(name)
            if st is None or st.pid == os.getpid():
                remove_run_state(name)
        return 0


def _setup_logging(name: str, to_stderr: bool) -> None:
    root = logging.getLogger("pairshell")
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    run_dir().mkdir(parents=True, exist_ok=True)
    fh = RotatingFileHandler(serve_log_path(name), maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    if to_stderr:
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(fmt)
        root.addHandler(sh)


def obtain_password(profile: Profile, interactive: bool) -> str | None:
    """Password for a telnet profile: env / credential store / prompt."""
    if not profile.needs_password:
        return None
    pw = resolve_password(profile.name)
    if pw is None and interactive and sys.stdin.isatty():
        import getpass

        pw = getpass.getpass(f"Password for {profile.label}: ")
    return pw


def run_serve(name: str, foreground: bool = True) -> int:
    """Entry point of ``pairshell serve``.  Returns the process exit code."""
    store = ProfileStore()
    profile = store.get(name)
    _setup_logging(name, to_stderr=foreground)
    password = obtain_password(profile, interactive=foreground)
    if profile.needs_password and password is None:
        log.error(
            "no password available for %s: run `pairshell edit %s` to store one, "
            "set %s, or run `pairshell serve %s` in a terminal to be prompted",
            name,
            name,
            "PAIRSHELL_PASSWORD",
            name,
        )
        return 2
    server = Server(profile, password)

    def _on_signal(signum: int, _frame: Any) -> None:
        log.info("signal %s received", signum)
        server.request_stop()

    for sig in ("SIGTERM", "SIGINT", "SIGBREAK", "SIGHUP"):
        if hasattr(signal, sig):
            try:
                signal.signal(getattr(signal, sig), _on_signal)
            except (ValueError, OSError):  # pragma: no cover - not main thread
                pass
    try:
        return server.run()
    except AuthError as exc:
        log.error("authentication failed: %s", exc)
        return 2
    except TransportError as exc:
        log.error("connection failed: %s", exc)
        return 2
    except KeyboardInterrupt:
        server.request_stop()
        return 0


# --------------------------------------------------------------------------
# Client-side helpers: start / find / stop a serve process
# --------------------------------------------------------------------------


def spawn_background(name: str) -> subprocess.Popen[bytes]:
    """Start ``pairshell serve <name>`` detached (hidden window on Windows)."""
    run_dir().mkdir(parents=True, exist_ok=True)
    # Crash output only; the rotating log handler writes <name>.log itself
    # (sharing one file would break rotation on Windows).
    logf = open(serve_stderr_path(name), "ab")
    argv = [sys.executable, "-m", "pairshell", "serve", "--background", name]
    # The child must import the same pairshell we are running (also when we
    # were started from a checkout via bin/pairshell rather than installed).
    env = dict(os.environ)
    pkg_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env["PYTHONPATH"] = pkg_root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    kwargs: dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": logf, "stderr": subprocess.STDOUT, "close_fds": True, "env": env}
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kwargs["start_new_session"] = True
    try:
        return subprocess.Popen(argv, **kwargs)
    finally:
        logf.close()


def log_tail(name: str, lines: int = 12) -> str:
    parts: list[str] = []
    for path in (serve_log_path(name), serve_stderr_path(name)):
        try:
            text = path.read_text(encoding="utf-8", errors="replace").rstrip()
        except OSError:
            continue
        if text:
            parts.append("\n".join(text.splitlines()[-lines:]))
    return "\n".join(parts)


def ping(state: RunState, timeout: float = PING_TIMEOUT) -> dict[str, Any] | None:
    try:
        return rpc.call(state.rpc_port, state.token, "ping", timeout=timeout, connect_timeout=timeout)
    except rpc.RpcError:
        return None


def find_running(name: str) -> RunState | None:
    """The live serve for ``name`` if its pid is alive and it answers a ping."""
    st = read_run_state(name)
    if st is None:
        return None
    if not run_state_is_ours(st):
        remove_run_state(name)
        return None
    if ping(st) is None:
        return None
    return st


def ensure_running(
    profile: Profile,
    announce: Callable[[str], None] | None = None,
    wait: float = START_WAIT,
) -> RunState:
    """Return a live serve for ``profile``, starting one when needed."""
    name = profile.name
    st = find_running(name)
    if st is not None:
        return st
    say = announce or (lambda _msg: None)
    lock = run_dir() / f"{name}.starting"
    run_dir().mkdir(parents=True, exist_ok=True)
    proc: subprocess.Popen[bytes] | None = None
    try:
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
        owner = True
    except FileExistsError:
        owner = False
        try:
            if time.time() - lock.stat().st_mtime > 120:
                lock.unlink()
                fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                owner = True
        except OSError:
            pass
    try:
        if owner:
            say(f"[pairshell] starting serve for {name} ({profile.protocol} {profile.label})...")
            proc = spawn_background(name)
        else:
            say(f"[pairshell] another process is starting serve for {name}; waiting...")
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            st = read_run_state(name)
            if st is not None and pid_alive(st.pid) and ping(st) is not None:
                return st
            if proc is not None and proc.poll() is not None:
                break
            time.sleep(0.25)
    finally:
        if owner:
            try:
                lock.unlink()
            except OSError:
                pass
    tail = log_tail(name)
    hint = f"Run `pairshell serve {name}` in a terminal to see what is wrong."
    if proc is not None and proc.poll() is not None:
        raise ServeError(f"serve for {name} exited with code {proc.returncode}.\n{tail}\n{hint}")
    raise ServeError(f"serve for {name} did not become ready within {wait:g}s.\n{tail}\n{hint}")


def stop_serve(name: str, grace: float = 5.0) -> bool:
    """Stop the serve process (never the remote tmux session).  True if one was running."""
    st = read_run_state(name)
    if st is None:
        return False
    if not run_state_is_ours(st):
        # dead, or a reused pid after a crash/reboot: never kill a stranger
        remove_run_state(name)
        return False
    try:
        rpc.call(st.rpc_port, st.token, "shutdown", timeout=3.0)
    except rpc.RpcError:
        pass
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if not pid_alive(st.pid):
            remove_run_state(name)
            return True
        time.sleep(0.1)
    from .profiles import kill_pid

    kill_pid(st.pid)
    remove_run_state(name)
    return True


def probe_state(profile: Profile, timeout: float = 1.5) -> dict[str, Any]:
    """Live state for ``list``/menu: stopped / connecting / idle / busy / error."""
    base = serve_state(profile.name)
    out: dict[str, Any] = {
        "running": bool(base["running"]),
        "pid": base["pid"],
        "rpc_port": base["rpc_port"] or profile.rpc_port,
        "state": "stopped",
        "detail": "",
    }
    if not base["running"]:
        return out
    st = read_run_state(profile.name)
    if st is None:
        return out
    try:
        info = rpc.call(st.rpc_port, st.token, "status", timeout=timeout, connect_timeout=timeout)
    except rpc.RpcError as exc:
        out["state"] = "connecting"
        out["detail"] = str(exc)
        return out
    if not info.get("ok"):
        out["state"] = "error"
        out["detail"] = info.get("error", "")
        return out
    out["state"] = "idle" if info.get("idle") else "busy"
    out["detail"] = info.get("busy_reason") or ""
    out["foreground"] = info.get("foreground")
    out["attached_clients"] = info.get("attached_clients")
    out["connected"] = info.get("connected")
    out["pending"] = info.get("pending")
    return out


__all__ = [
    "START_WAIT",
    "ServeError",
    "Server",
    "ensure_running",
    "find_running",
    "log_tail",
    "obtain_password",
    "ping",
    "probe_state",
    "run_serve",
    "spawn_background",
    "stop_serve",
]
