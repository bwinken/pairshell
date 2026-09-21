"""Connection profiles, the ``current`` marker and per-profile run state.

Layout (``%APPDATA%\\pairshell`` on Windows, ``$XDG_CONFIG_HOME/pairshell``
elsewhere, or ``$PAIRSHELL_HOME`` when set)::

    profiles.json      all profiles
    current            name of the profile last connected from the menu
    run/<name>.json    pid, rpc_port, token, started_at of a live serve
    run/<name>.log     serve log
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

PROTOCOLS = ("telnet", "ssh", "local")
PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
RPC_PORT_RANGE = range(47100, 48000)
DEFAULT_PORTS = {"telnet": 23, "ssh": 22, "local": 0}


class ProfileError(Exception):
    pass


class ProfileNotFound(ProfileError):
    pass


# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------


def config_dir() -> Path:
    env = os.environ.get("PAIRSHELL_HOME")
    if env:
        return Path(env).expanduser()
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "pairshell"
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "pairshell"


def run_dir() -> Path:
    return config_dir() / "run"


def profiles_path() -> Path:
    return config_dir() / "profiles.json"


def current_path() -> Path:
    return config_dir() / "current"


def serve_log_path(name: str) -> Path:
    return run_dir() / f"{name}.log"


def serve_stderr_path(name: str) -> Path:
    """Where a background serve's own stderr goes (tracebacks, nothing else)."""
    return run_dir() / f"{name}.stderr.log"


def _atomic_write(path: Path, data: str, private: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
        if private and sys.platform != "win32":
            os.chmod(tmp, 0o600)
        for attempt in range(10):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                # Windows: another pairshell process is reading the file right now.
                if attempt == 9:
                    raise
                time.sleep(0.05)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------
# Profiles
# --------------------------------------------------------------------------


def validate_profile_name(name: str) -> str:
    if not PROFILE_NAME_RE.match(name or ""):
        raise ProfileError(f"invalid profile name {name!r}: letters, digits, '_', '.', '-' (max 64)")
    return name


def sanitize_session_name(name: str) -> str:
    """tmux forbids '.' and ':' in session names."""
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "_", name)
    if not cleaned or not re.match(r"[A-Za-z0-9_]", cleaned[0]):
        cleaned = "s_" + cleaned
    return cleaned


@dataclass
class Profile:
    name: str
    protocol: str = "ssh"
    host: str = ""
    port: int = 0
    user: str = ""
    key_path: str = ""
    session: str = ""
    rpc_port: int = 0
    last_used: float = 0.0
    ssh_options: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.port:
            self.port = DEFAULT_PORTS.get(self.protocol, 0)
        if not self.session:
            self.session = sanitize_session_name(self.name)

    def validate(self) -> "Profile":
        validate_profile_name(self.name)
        if self.protocol not in PROTOCOLS:
            raise ProfileError(f"protocol must be one of {', '.join(PROTOCOLS)}")
        if self.protocol != "local" and not self.host:
            raise ProfileError("host is required")
        if self.protocol != "local" and not self.user:
            raise ProfileError("user is required")
        if self.protocol != "local" and not (0 < int(self.port) < 65536):
            raise ProfileError("port must be 1-65535")
        from .tmuxops import validate_session_name

        try:
            validate_session_name(self.session)
        except ValueError as exc:
            raise ProfileError(str(exc)) from None
        return self

    @property
    def label(self) -> str:
        if self.protocol == "local":
            return "this machine"
        return f"{self.user}@{self.host}:{self.port}"

    @property
    def needs_password(self) -> bool:
        return self.protocol == "telnet"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Profile":
        known = {f: d.get(f) for f in cls.__dataclass_fields__ if f in d}
        known.setdefault("name", d.get("name", ""))
        if known.get("ssh_options") is None:
            known["ssh_options"] = []
        known["port"] = int(known.get("port") or 0)
        known["rpc_port"] = int(known.get("rpc_port") or 0)
        known["last_used"] = float(known.get("last_used") or 0.0)
        return cls(**known)  # type: ignore[arg-type]


class ProfileStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or profiles_path()

    def load(self) -> dict[str, Profile]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except ValueError as exc:
            raise ProfileError(f"{self.path} is not valid JSON: {exc}") from exc
        profiles = raw.get("profiles", {}) if isinstance(raw, dict) else {}
        out: dict[str, Profile] = {}
        for name, d in profiles.items():
            if isinstance(d, dict):
                d = dict(d)
                d.setdefault("name", name)
                out[name] = Profile.from_dict(d)
        return out

    def save(self, profiles: dict[str, Profile]) -> None:
        data = {"version": 1, "profiles": {n: p.to_dict() for n, p in sorted(profiles.items())}}
        _atomic_write(self.path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")

    def names(self) -> list[str]:
        return sorted(self.load())

    def list(self) -> list[Profile]:
        """Profiles sorted by ``last_used`` (most recent first), then name."""
        return sorted(self.load().values(), key=lambda p: (-p.last_used, p.name))

    def get(self, name: str) -> Profile:
        profiles = self.load()
        if name not in profiles:
            raise ProfileNotFound(f"no profile named {name!r} (see `pairshell list`)")
        return profiles[name]

    def exists(self, name: str) -> bool:
        return name in self.load()

    def allocate_rpc_port(self, profiles: dict[str, Profile], exclude: str | None = None) -> int:
        used = {p.rpc_port for n, p in profiles.items() if n != exclude}
        for port in RPC_PORT_RANGE:
            if port not in used:
                return port
        raise ProfileError("no free RPC port left")

    def upsert(self, profile: Profile) -> Profile:
        profile.validate()
        profiles = self.load()
        others = {n: p for n, p in profiles.items() if n != profile.name}
        if not profile.rpc_port or any(p.rpc_port == profile.rpc_port for p in others.values()):
            profile.rpc_port = self.allocate_rpc_port(others)
        profiles[profile.name] = profile
        self.save(profiles)
        return profile

    def remove(self, name: str) -> Profile:
        profiles = self.load()
        if name not in profiles:
            raise ProfileNotFound(f"no profile named {name!r}")
        removed = profiles.pop(name)
        self.save(profiles)
        if get_current() == name:
            clear_current()
        return removed

    def touch(self, name: str) -> None:
        profiles = self.load()
        if name in profiles:
            profiles[name].last_used = time.time()
            self.save(profiles)

    def set_rpc_port(self, name: str, port: int) -> None:
        profiles = self.load()
        if name in profiles:
            profiles[name].rpc_port = int(port)
            self.save(profiles)


# --------------------------------------------------------------------------
# current
# --------------------------------------------------------------------------


def get_current() -> str | None:
    try:
        name = current_path().read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    return name or None


def set_current(name: str) -> None:
    _atomic_write(current_path(), name + "\n")


def clear_current() -> None:
    try:
        current_path().unlink()
    except FileNotFoundError:
        pass


# --------------------------------------------------------------------------
# run state
# --------------------------------------------------------------------------


@dataclass
class RunState:
    pid: int
    rpc_port: int
    token: str
    started_at: float
    profile: str = ""
    transport: str = ""


def run_state_path(name: str) -> Path:
    return run_dir() / f"{name}.json"


def write_run_state(state: RunState) -> None:
    _atomic_write(run_state_path(state.profile), json.dumps(asdict(state)) + "\n", private=True)


def read_run_state(name: str) -> RunState | None:
    try:
        d = json.loads(run_state_path(name).read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return None
    try:
        return RunState(
            pid=int(d["pid"]),
            rpc_port=int(d["rpc_port"]),
            token=str(d["token"]),
            started_at=float(d.get("started_at", 0)),
            profile=str(d.get("profile", name)),
            transport=str(d.get("transport", "")),
        )
    except (KeyError, TypeError, ValueError):
        return None


def remove_run_state(name: str) -> None:
    try:
        run_state_path(name).unlink()
    except FileNotFoundError:
        pass


def _kernel32() -> Any:  # pragma: no cover - Windows only
    """kernel32 with explicit signatures (HANDLE is 64-bit; never let ctypes guess)."""
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    k32.GetExitCodeProcess.restype = wintypes.BOOL
    k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    k32.TerminateProcess.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.restype = wintypes.BOOL
    return k32


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = _kernel32()
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A killed serve whose parent is gone may linger as a zombie until init
    # reaps it; that is not a live process.
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            stat = fh.read()
        state = stat[stat.rindex(b")") + 2 : stat.rindex(b")") + 3]
        if state in (b"Z", b"X"):
            return False
    except (OSError, ValueError):
        pass
    return True


def process_start_time(pid: int) -> float | None:
    """Creation time of ``pid`` as epoch seconds, or None when unknown."""
    if sys.platform == "win32":  # pragma: no cover - Windows only
        from ctypes import wintypes

        class FILETIME(ctypes.Structure):
            _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

        k32 = _kernel32()
        k32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(FILETIME)] * 4
        k32.GetProcessTimes.restype = wintypes.BOOL
        handle = k32.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return None
        try:
            created, exited, kernel, user = FILETIME(), FILETIME(), FILETIME(), FILETIME()
            if not k32.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user)):
                return None
            ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
            return ticks / 10_000_000 - 11644473600
        finally:
            k32.CloseHandle(handle)
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            stat = fh.read()
        fields = stat[stat.rindex(b")") + 2 :].split()
        start_ticks = int(fields[19])  # field 22: starttime, clock ticks since boot
        with open("/proc/stat", "rb") as fh:
            btime = next(int(line.split()[1]) for line in fh if line.startswith(b"btime"))
        return btime + start_ticks / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError, StopIteration, AttributeError):
        return None


def run_state_is_ours(state: "RunState", tolerance: float = 120.0) -> bool:
    """True when ``state.pid`` is alive and is the serve that wrote the file.

    Run-state files survive crashes and reboots, and pids get reused, so the
    process creation time is compared with the recorded ``started_at``
    (captured when the serve process imported pairshell).  When the creation
    time cannot be determined the pid check alone decides.
    """
    if not pid_alive(state.pid):
        return False
    created = process_start_time(state.pid)
    if created is None or not state.started_at:
        return True
    return abs(created - state.started_at) <= tolerance


def kill_pid(pid: int) -> None:
    """Terminate a serve process that did not stop gracefully."""
    if sys.platform == "win32":
        PROCESS_TERMINATE = 0x0001
        kernel32 = _kernel32()
        handle = kernel32.OpenProcess(PROCESS_TERMINATE, False, int(pid))
        if handle:
            try:
                kernel32.TerminateProcess(handle, 1)
            finally:
                kernel32.CloseHandle(handle)
        return
    import signal

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    for _ in range(20):
        if not pid_alive(pid):
            return
        time.sleep(0.1)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def serve_state(name: str) -> dict[str, Any]:
    """What we know about the serve process for ``name`` without talking to it."""
    st = read_run_state(name)
    if st is None:
        return {"running": False, "stale": False, "pid": None, "rpc_port": None, "token": None, "started_at": None}
    alive = run_state_is_ours(st)
    if not alive:
        remove_run_state(name)
    return {
        "running": alive,
        "stale": not alive,
        "pid": st.pid,
        "rpc_port": st.rpc_port,
        "token": st.token,
        "started_at": st.started_at,
        "transport": st.transport,
    }


__all__ = [
    "PROTOCOLS",
    "Profile",
    "ProfileError",
    "ProfileNotFound",
    "ProfileStore",
    "RunState",
    "clear_current",
    "config_dir",
    "get_current",
    "kill_pid",
    "pid_alive",
    "process_start_time",
    "profiles_path",
    "read_run_state",
    "remove_run_state",
    "run_dir",
    "run_state_is_ours",
    "sanitize_session_name",
    "serve_log_path",
    "serve_state",
    "serve_stderr_path",
    "set_current",
    "validate_profile_name",
    "write_run_state",
]
