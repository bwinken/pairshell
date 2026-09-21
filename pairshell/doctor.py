"""``pairshell doctor <profile>``: step-by-step connection diagnostics.

Prints one line per phase so a failed connection points at the culprit
(network, credentials, login prompts, bash, control channel, tmux) instead of
a single "did not connect".  Never prints the password.
"""

from __future__ import annotations

import os
import re
import socket
import sys
import time
from typing import Callable

from . import __version__, credentials
from .profiles import Profile, config_dir, run_dir, serve_state
from .serve import log_tail
from .transports.base import AuthError, ByteStream, ControlTimeout, TransportError
from .transports.telnet import LOGIN_PROMPT, PASSWORD_PROMPT, TelnetSession, telnet_login

Report = Callable[[str, str, str], None]


def _tail(data: bytes, n: int = 300) -> str:
    text = data.decode("utf-8", "replace").replace("\r", "")
    text = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", text)
    return text[-n:].strip()


def run_doctor(profile: Profile, password: str | None, out=None, login_timeout: float = 40.0) -> int:
    """Return 0 when every check passed, 1 otherwise."""
    out = out or sys.stdout
    failures = 0

    def say(status: str, what: str, detail: str = "") -> None:
        nonlocal failures
        if status == "FAIL":
            failures += 1
        line = f"[{status:^4}] {what}"
        if detail:
            line += f": {detail}"
        print(line, file=out, flush=True)

    say(" ok ", "pairshell", f"{__version__}, python {sys.version.split()[0]}, {sys.platform}")
    say(" ok ", "config dir", str(config_dir()))
    say(" ok ", "run dir", str(run_dir()))
    say(" ok ", "profile", f"{profile.name}: {profile.protocol} {profile.label}, tmux session '{profile.session}'")

    st = serve_state(profile.name)
    if st["running"]:
        say("info", "serve", f"running (pid {st['pid']}, rpc {st['rpc_port']})")
    else:
        say("info", "serve", "not running")
    tail = log_tail(profile.name, 6)
    if tail:
        say("info", "last serve log lines", "\n" + "\n".join("        " + ln for ln in tail.splitlines()))

    if profile.protocol == "local":
        say("info", "local profile", "nothing to connect to")
        return 0

    # credentials
    if profile.needs_password:
        backend = credentials.backend()
        if password:
            say(" ok ", "password", f"available ({backend}{' or env' if os.environ.get(credentials.ENV_PASSWORD) else ''})")
        else:
            say("FAIL", "password", f"none stored ({backend}); run `pairshell edit {profile.name}`")
    else:
        from .transports.ssh import find_ssh

        exe = find_ssh()
        say(" ok " if exe else "FAIL", "ssh.exe", exe or "not found (install the Windows OpenSSH Client feature)")
        if profile.key_path:
            exists = os.path.exists(os.path.expanduser(profile.key_path))
            say(" ok " if exists else "FAIL", "key file", profile.key_path + ("" if exists else " (missing)"))

    # network
    t0 = time.monotonic()
    try:
        socket.create_connection((profile.host, int(profile.port)), timeout=5).close()
        say(" ok ", "tcp connect", f"{profile.host}:{profile.port} in {time.monotonic() - t0:.2f}s")
    except OSError as exc:
        say("FAIL", "tcp connect", f"{profile.host}:{profile.port}: {exc}")
        return 1

    if profile.protocol != "telnet":
        say("info", "ssh", f"try: pairshell serve {profile.name}  (shows ssh.exe's own messages)")
        return 1 if failures else 0
    if not password:
        return 1

    # telnet, phase by phase
    session = TelnetSession(profile.host, int(profile.port), term="dumb", size=(200, 50))
    try:
        session.open()
    except TransportError as exc:
        say("FAIL", "telnet open", str(exc))
        return 1
    stream = ByteStream(session.read_some, session.write, session.close, name="telnet").start()
    try:
        try:
            before, _ = stream.read_until(LOGIN_PROMPT, 25.0)
            say(" ok ", "login prompt", f"seen after {len(before)} bytes of banner")
        except ControlTimeout:
            say("FAIL", "login prompt", "not seen within 25s; received: " + repr(_tail(stream.drain())))
            return 1
        stream.write(profile.user.encode("utf-8") + b"\n")
        try:
            stream.read_until(PASSWORD_PROMPT, 15.0)
            say(" ok ", "password prompt", "seen")
        except ControlTimeout:
            say("info", "password prompt", "not seen within 15s (passwordless account?); received: " + repr(_tail(stream.drain())))
        stream.write(password.encode("utf-8") + b"\n")
        data, matched = stream.wait_quiet(min_quiet=1.0, max_total=10.0, stop_pattern=re.compile(rb"(?i)login incorrect|authentication failure|access denied|login failed"))
        if matched:
            say("FAIL", "authentication", "remote reported bad credentials")
            return 1
        say(" ok ", "authentication", "no failure message; " + repr(_tail(data, 160)))
        stream.write(b"exec bash --norc --noprofile\n")
        stream.wait_quiet(min_quiet=1.0, max_total=3.0)
        stream.drain()
        from .transports.telnet import TelnetTransport

        probe = TelnetTransport(profile.host, int(profile.port), profile.user, password)
        try:
            probe._initialise(stream)  # setup line + sync sentinel
            say(" ok ", "control shell", "bash answered the sync sentinel")
        except TransportError as exc:
            say("FAIL", "control shell", f"{exc}; received: {_tail(stream.drain())!r}")
            return 1
        probe._stream = stream
        rc, text = probe._run_once(stream, "tmux -V; command -v base64; echo shell=$SHELL", 15.0)
        text = text.strip()
        say(" ok " if "tmux" in text else "FAIL", "remote tmux", text.splitlines()[0] if text else "no output")
        say(" ok " if "base64" in text else "FAIL", "remote base64", "found" if "base64" in text else "missing (coreutils)")
        m = re.search(r"shell=(\S+)", text)
        say("info", "login shell", m.group(1) if m else "unknown")
    except AuthError as exc:
        say("FAIL", "authentication", str(exc))
        return 1
    except TransportError as exc:
        say("FAIL", "connection", str(exc))
        return 1
    finally:
        stream.close()
    return 1 if failures else 0


__all__ = ["run_doctor"]
