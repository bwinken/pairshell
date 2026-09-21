"""Everything pairshell does to the shared tmux pane.

The pure functions at the top (sentinels, idle detection, capture-window
math, output extraction) are unit-tested against recorded tmux output; the
:class:`TmuxSession` class runs them over a :class:`Transport`.

Command execution protocol (see README):

1. **Idle check** - ``tmux display -p`` + ``capture-pane`` (no ``-J`` so line
   indices match ``cursor_y``).  Idle = foreground process is a shell and the
   cursor line ends like a prompt.  Otherwise refuse with rc 3.
2. **Send** - append ``; echo __DONE_"$?"_<nonce>__`` (``$status`` for csh
   family) and type it with ``send-keys -l`` via a base64 round trip.
3. **Wait** - poll the visible pane with exponential backoff until the
   sentinel regex matches.  The echoed command line shows the literal
   ``"$?"`` so only real execution prints digits.
4. **Collect** - ``capture-pane -J`` from the absolute line recorded before
   sending; output is what lies between the echoed line and the sentinel.
5. **Timeout** - rc 124, partial output; the command keeps running.
"""

from __future__ import annotations

import base64
import logging
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from .transports.base import Transport, TransportError

log = logging.getLogger("pairshell.tmux")

# Exit codes reported by ``exec``
RC_BUSY = 3
RC_TIMEOUT = 124
RC_NO_SENTINEL = 125

SHELLS = {"sh", "bash", "dash", "ash", "ksh", "mksh", "pdksh", "zsh", "csh", "tcsh", "fish"}
CSH_FAMILY = {"csh", "tcsh"}
# The spec's `[%$#>]` plus the glyphs popular prompt themes end with.
PROMPT_RE = re.compile(r"[%$#>\u276f\u279c\u03bb\u00bb\u2192]\s*$")
KEY_NAME_RE = re.compile(r"^[A-Za-z0-9_^\-]+$")
SESSION_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]*$")
NONCE_RE = re.compile(r"^[0-9a-f]{8}$")

CONTROL_TIMEOUT = 15.0
POLL_INITIAL = 0.3
POLL_MAX = 2.0
POLL_FACTOR = 1.5
SCREEN_TAIL_LINES = 15


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------


def normalize_command(cmd: str) -> str:
    """``/usr/bin/-tcsh`` -> ``tcsh`` (strip path and login-shell dash)."""
    cmd = (cmd or "").strip()
    if "/" in cmd:
        cmd = cmd.rsplit("/", 1)[-1]
    if cmd.startswith("-"):
        cmd = cmd[1:]
    return cmd


def is_shell(cmd: str) -> bool:
    return normalize_command(cmd) in SHELLS


def shell_family(cmd: str) -> str:
    """``"csh"`` for csh/tcsh, ``"fish"`` for fish, else ``"sh"``."""
    name = normalize_command(cmd)
    if name in CSH_FAMILY:
        return "csh"
    if name == "fish":
        return "fish"
    return "sh"


def new_nonce() -> str:
    return secrets.token_hex(4)


def done_pattern(nonce: str) -> re.Pattern[str]:
    return re.compile(r"__DONE_(\d+)_" + re.escape(nonce) + r"__")


def echo_marker(nonce: str) -> str:
    """Substring present on the echoed command line (and in the sentinel)."""
    return "_" + nonce + "__"


def sentinel_text(family: str, nonce: str) -> str:
    var = "$status" if family in ("csh", "fish") else "$?"
    return f'echo __DONE_"{var}"_{nonce}__'


def build_command_line(cmd: str, family: str, nonce: str) -> str:
    """The exact text typed into the pane for ``cmd``."""
    stripped = cmd.rstrip()
    sep = " " if stripped.endswith("&") else " ; "
    return stripped + sep + sentinel_text(family, nonce)


def b64_shell_arg(text: str) -> str:
    """A bash expression that yields ``text`` verbatim through base64.

    ``$(...)`` strips trailing newlines, which is fine: newlines are rejected
    upstream.  Nothing from ``text`` can reach either shell unquoted.
    """
    payload = base64.b64encode(text.encode("utf-8")).decode("ascii")
    return f"\"$(printf %s '{payload}' | base64 -d)\""


def literal_for_send_keys(text: str) -> str:
    """Protect a ``send-keys -l`` argument from tmux's ``;`` command separator.

    tmux treats an argument that ends in ``;`` as the end of a command; a
    trailing ``\;`` becomes a literal ``;``.
    """
    if text.endswith(";"):
        return text[:-1] + "\;"
    return text


def validate_key_name(name: str) -> str:
    if not KEY_NAME_RE.match(name):
        raise ValueError(f"invalid key name {name!r}: use names like C-c, Enter, Up, q; free text needs --literal")
    return name


def validate_literal(text: str) -> str:
    if "\n" in text or "\r" in text:
        raise ValueError("--literal text must not contain newlines; add Enter as a separate key")
    if "\0" in text:
        raise ValueError("--literal text must not contain NUL")
    return text


def validate_exec_command(cmd: str) -> str:
    if "\n" in cmd or "\r" in cmd:
        raise ValueError("one line per exec argument: newlines are not allowed")
    if "\0" in cmd:
        raise ValueError("command must not contain NUL")
    if not cmd.strip():
        raise ValueError("empty command")
    return cmd


def validate_session_name(name: str) -> str:
    if not SESSION_NAME_RE.match(name):
        raise ValueError(f"invalid tmux session name {name!r}: use letters, digits, '_' and '-'")
    return name


def shell_quote(text: str) -> str:
    """POSIX single-quote ``text``."""
    return "'" + text.replace("'", "'\\''") + "'"


@dataclass
class PaneState:
    history_size: int = 0
    cursor_y: int = 0
    foreground: str = ""
    attached: int = 0
    width: int = 0
    height: int = 0
    window_index: int = 0
    pane_index: int = 0
    in_mode: int = 0
    lines: list[str] = field(default_factory=list)

    @property
    def cursor_line(self) -> str:
        if 0 <= self.cursor_y < len(self.lines):
            return self.lines[self.cursor_y]
        return ""

    @property
    def start_line(self) -> int:
        """Absolute grid index of the cursor line (history + visible offset)."""
        return self.history_size + self.cursor_y


DISPLAY_FORMAT = (
    "#{history_size} #{cursor_y} #{session_attached} #{pane_width} #{pane_height} "
    "#{window_index} #{pane_index} #{pane_in_mode} #{pane_current_command}"
)
DISPLAY_SEPARATOR = "__PAIRSHELL_CAPTURE__"


def parse_pane_state(out: str) -> PaneState:
    """Parse the combined output of ``display -p`` + separator + ``capture-pane``."""
    head, sep, body = out.partition(DISPLAY_SEPARATOR + "\n")
    if not sep:
        head, sep, body = out.partition(DISPLAY_SEPARATOR)
    head = head.strip("\n")
    if not head:
        raise TransportError("tmux display-message produced no output")
    first = head.splitlines()[-1]
    parts = first.split(" ", 8)
    if len(parts) < 9:
        raise TransportError(f"unexpected tmux display output: {first!r}")
    try:
        nums = [int(x) for x in parts[:8]]
    except ValueError as exc:
        raise TransportError(f"unexpected tmux display output: {first!r}") from exc
    lines = [ln.rstrip() for ln in body.split("\n")]
    if lines and lines[-1] == "":
        lines.pop()
    return PaneState(
        history_size=nums[0],
        cursor_y=nums[1],
        attached=nums[2],
        width=nums[3],
        height=nums[4],
        window_index=nums[5],
        pane_index=nums[6],
        in_mode=nums[7],
        foreground=parts[8].strip(),
        lines=lines,
    )


def idle_reason(state: PaneState) -> str | None:
    """``None`` when the pane is idle, else a human-readable reason."""
    if state.in_mode:
        return "pane is in copy/view mode (the user is scrolling)"
    if not is_shell(state.foreground):
        return f"a program is running in the foreground: {state.foreground or '?'}"
    if not PROMPT_RE.search(state.cursor_line):
        return "cursor line does not look like a shell prompt (the user may be typing)"
    return None


def find_done(lines: list[str], nonce: str) -> int | None:
    """Exit code if the sentinel is visible in ``lines``, tolerant of wrapping."""
    pat = done_pattern(nonce)
    m = pat.search("\n".join(lines))
    if m is None:
        # A sentinel that wrapped onto a second row still reads across rows.
        m = pat.search("".join(lines))
    return int(m.group(1)) if m else None


def compute_capture_start(start_line: int, history_size_now: int) -> int:
    """Relative ``-S`` index for the absolute ``start_line``, clamped to history."""
    rel = start_line - history_size_now
    return max(rel, -history_size_now)


@dataclass
class Extracted:
    lines: list[str]
    rc: int | None
    found_echo: bool
    found_done: bool


def extract_output(captured: list[str], nonce: str) -> Extracted:
    """Cut the command output out of a ``-J`` capture that starts at the prompt line.

    * echo line = first line containing the nonce (the typed command),
    * sentinel line = first ``__DONE_<rc>_<nonce>__`` after it,
    * output = the lines strictly between, plus any text that precedes the
      sentinel on its own line (commands that print no trailing newline).
    """
    marker = echo_marker(nonce)
    pat = done_pattern(nonce)
    echo_idx: int | None = None
    done_idx: int | None = None
    done_m: re.Match[str] | None = None
    for i, line in enumerate(captured):
        m = pat.search(line)
        if m is not None and (echo_idx is None or i > echo_idx):
            done_idx, done_m = i, m
            break
        if echo_idx is None and marker in line:
            echo_idx = i
    if echo_idx is not None:
        first = echo_idx + 1
    else:
        # The shell did not echo (or history was wiped): skip the prompt line.
        first = 1 if (done_idx is None or done_idx > 0) else 0
    if done_idx is None:
        out = [ln.rstrip() for ln in captured[first:]]
        while out and not out[-1]:
            out.pop()  # the empty rows below the cursor are not output
        return Extracted(out, None, echo_idx is not None, False)
    out = [ln.rstrip() for ln in captured[first:done_idx]]
    assert done_m is not None
    prefix = captured[done_idx][: done_m.start()]
    if prefix:
        out.append(prefix.rstrip())
    return Extracted(out, int(done_m.group(1)), echo_idx is not None, True)


def truncate_lines(lines: list[str], max_lines: int) -> tuple[list[str], int]:
    """Keep the last ``max_lines`` lines; return ``(kept, omitted_count)``."""
    if max_lines <= 0 or len(lines) <= max_lines:
        return lines, 0
    omitted = len(lines) - max_lines
    return lines[omitted:], omitted


def screen_tail(lines: list[str], n: int = SCREEN_TAIL_LINES) -> list[str]:
    trimmed = list(lines)
    while trimmed and not trimmed[-1].strip():
        trimmed.pop()
    return trimmed[-n:]


# --------------------------------------------------------------------------
# tmux operations over a transport
# --------------------------------------------------------------------------


class TmuxSession:
    """Drives one tmux session (the shared shell) through a control transport."""

    def __init__(self, transport: Transport, session: str, log_dir: str = "~/.pairshell") -> None:
        self.transport = transport
        self.session = validate_session_name(session)
        self.log_dir = log_dir
        self._exec_lock = threading.Lock()
        self._default_family: str | None = None
        self.last_activity = time.monotonic()

    # -- low level -------------------------------------------------------------

    @property
    def target(self) -> str:
        """``=name:`` = exact session match, its current window, active pane.

        A bare name would also prefix-match ``name2``; a bare ``=name`` is
        rejected by pane commands such as ``send-keys`` on tmux 3.x.
        """
        return "=" + self.session + ":"

    def _tmux(self, cmd: str, timeout: float = CONTROL_TIMEOUT) -> tuple[int, str]:
        self.last_activity = time.monotonic()
        return self.transport.run(cmd, timeout)

    def _check(self, cmd: str, timeout: float = CONTROL_TIMEOUT) -> str:
        rc, out = self._tmux(cmd, timeout)
        if rc != 0:
            raise TransportError(f"tmux command failed (rc={rc}): {out.strip()[-500:]}")
        return out

    def ensure(self) -> bool:
        """Create the session if needed and keep a transcript.  Returns True if created.

        Idempotent: safe to run before every client call.
        """
        s = self.session
        t = shell_quote(self.target)
        logdir = self.log_dir
        cmd = (
            f"mkdir -p {logdir} 2>/dev/null; "
            f"if tmux has-session -t {t} 2>/dev/null; then echo existing; else "
            f"tmux start-server \; set-option -g history-limit 50000 \; "
            f"new-session -d -s {shell_quote(s)} -x 200 -y 50 \; "
            f"send-keys -t {t} 'unset autologout' Enter && echo created; fi; "
            # `pipe-pane -o` toggles (it closes an existing pipe first), so
            # check #{pane_pipe} ourselves to keep this idempotent.
            f"if [ \"$(tmux display -p -t {t} '#{{pane_pipe}}')\" != 1 ]; then "
            f"tmux pipe-pane -t {t} {shell_quote(f'cat >> {logdir}/{s}.log')}; fi"
        )
        out = self._check(cmd)
        created = "created" in out
        if created:
            log.info("created tmux session %s", s)
            self._wait_first_prompt()
        return created

    def _wait_first_prompt(self, timeout: float = 5.0) -> None:
        """Give a freshly created pane time to show its prompt.

        Without this the first ``exec`` after creation would see a pane whose
        shell is still starting and wrongly report it busy (rc 3).
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if idle_reason(self.inspect()) is None:
                    return
            except TransportError:
                return
            time.sleep(0.2)

    def inspect(self) -> PaneState:
        """Current pane metrics plus the visible screen (no -J)."""
        t = shell_quote(self.target)
        out = self._check(
            f"tmux display -p -t {t} {shell_quote(DISPLAY_FORMAT)} && echo {DISPLAY_SEPARATOR} && tmux capture-pane -p -t {t}"
        )
        return parse_pane_state(out)

    def default_shell_family(self) -> str:
        if self._default_family is None:
            rc, out = self._tmux("tmux show-option -gqv default-shell; echo; echo $SHELL")
            fam = "sh"
            for cand in out.split():
                if is_shell(cand):
                    fam = shell_family(cand)
                    break
            self._default_family = fam
        return self._default_family

    def capture_from(self, start_line: int) -> list[str]:
        """``capture-pane -J`` from an absolute grid line to the bottom of the pane."""
        t = shell_quote(self.target)
        out = self._check(
            f"hs=$(tmux display -p -t {t} '#{{history_size}}'); s=$(({start_line} - hs)); "
            f"if [ \"$s\" -lt \"-$hs\" ]; then s=-$hs; fi; tmux capture-pane -p -J -t {t} -S \"$s\""
        )
        lines = out.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        return lines

    def send_text(self, text: str, enter: bool = False) -> None:
        """Type ``text`` literally into the pane (base64 round trip, no quoting hazards)."""
        t = shell_quote(self.target)
        cmd = f"tmux send-keys -t {t} -l -- {b64_shell_arg(literal_for_send_keys(text))}"
        if enter:
            cmd += f" && tmux send-keys -t {t} Enter"
        self._check(cmd)

    def send_keys(self, items: list[tuple[str, str]]) -> None:
        """``items`` are ``("key", name)`` or ``("literal", text)`` in order."""
        t = shell_quote(self.target)
        parts: list[str] = []
        pending: list[str] = []

        def flush() -> None:
            if pending:
                parts.append(f"tmux send-keys -t {t} " + " ".join(pending))
                pending.clear()

        for kind, value in items:
            if kind == "key":
                pending.append(validate_key_name(value))
            else:
                flush()
                parts.append(f"tmux send-keys -t {t} -l -- {b64_shell_arg(literal_for_send_keys(validate_literal(value)))}")
        flush()
        if parts:
            self._check(" && ".join(parts))

    # -- high level -------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        state = self.inspect()
        reason = idle_reason(state)
        return {
            "session": self.session,
            "foreground": state.foreground,
            "shell_family": shell_family(state.foreground) if is_shell(state.foreground) else None,
            "idle": reason is None,
            "busy_reason": reason,
            "cursor_line": state.cursor_line,
            "attached_clients": state.attached,
            "window": f"{state.window_index}.{state.pane_index}",
            "size": f"{state.width}x{state.height}",
            "history_size": state.history_size,
            "in_mode": bool(state.in_mode),
        }

    def screen(self, scrollback: int = 0) -> dict[str, Any]:
        t = shell_quote(self.target)
        n = max(0, int(scrollback))
        out = self._check(
            f"tmux display -p -t {t} {shell_quote(DISPLAY_FORMAT)} && echo {DISPLAY_SEPARATOR} && "
            f"tmux capture-pane -p -t {t} -S -{n}"
        )
        state = parse_pane_state(out)
        reason = idle_reason(state)
        lines = list(state.lines)
        while lines and not lines[-1]:
            lines.pop()  # the unused rows at the bottom of the pane
        return {
            "lines": lines,
            "foreground": state.foreground,
            "idle": reason is None,
            "busy_reason": reason,
            "attached_clients": state.attached,
            "window": f"{state.window_index}.{state.pane_index}",
        }

    def keys(self, items: list[tuple[str, str]], settle: float = 0.3) -> dict[str, Any]:
        self.send_keys(items)
        time.sleep(settle)
        return self.screen(0)

    def ctl(self, cmd: str, timeout: float = 30.0) -> dict[str, Any]:
        rc, out = self._tmux(cmd, timeout)
        return {"rc": rc, "output": out}

    def exec(self, cmd: str, timeout: float = 120.0, force: bool = False, max_lines: int = 500) -> dict[str, Any]:
        """Run ``cmd`` in the shared pane.  See the module docstring for the protocol."""
        validate_exec_command(cmd)
        nonce = new_nonce()
        with self._exec_lock:
            state = self.inspect()
            reason = idle_reason(state)
            if reason is not None and not force:
                return {
                    "status": "busy",
                    "rc": RC_BUSY,
                    "reason": reason,
                    "foreground": state.foreground,
                    "cursor_line": state.cursor_line,
                    "screen_tail": screen_tail(state.lines),
                    "output": [],
                    "omitted": 0,
                    "nonce": nonce,
                }
            family = shell_family(state.foreground) if is_shell(state.foreground) else self.default_shell_family()
            line = build_command_line(cmd, family, nonce)
            start_line = state.start_line
            self.send_text(line, enter=True)
        log.info("exec[%s] %s", nonce, cmd[:200])

        deadline = time.monotonic() + max(0.0, timeout)
        delay = POLL_INITIAL
        idle_polls = 0
        while True:
            try:
                st = self.inspect()
            except TransportError as exc:
                if "can't find" in str(exc) or "no server" in str(exc) or "no such" in str(exc):
                    return self._finish(nonce, "no_session", RC_NO_SENTINEL, [], 0, cmd, note=f"the tmux session went away: {exc}")
                raise
            rc = find_done(st.lines, nonce)
            if rc is not None:
                captured = self.capture_from(start_line)
                ext = extract_output(captured, nonce)
                out, omitted = truncate_lines(ext.lines, max_lines)
                return self._finish(nonce, "done", ext.rc if ext.rc is not None else rc, out, omitted, cmd)
            if idle_reason(st) is None:
                # A prompt is back but no sentinel is visible.  Right after the
                # command finishes there is a tiny window before the echo lands,
                # so this only counts once it persists across two polls.
                idle_polls += 1
            else:
                idle_polls = 0
            if idle_polls >= 2:
                # Back at a prompt for two polls without a sentinel on screen:
                # confirm against the full capture (it may have scrolled).
                captured = self.capture_from(start_line)
                ext = extract_output(captured, nonce)
                if ext.found_done and ext.rc is not None:
                    out, omitted = truncate_lines(ext.lines, max_lines)
                    return self._finish(nonce, "done", ext.rc, out, omitted, cmd)
                out, omitted = truncate_lines(ext.lines, max_lines)
                return self._finish(
                    nonce,
                    "no_sentinel",
                    RC_NO_SENTINEL,
                    out,
                    omitted,
                    cmd,
                    note="the shell is back at a prompt but never printed the sentinel "
                    "(syntax error rejected the whole line, `exec`, a sub-shell, or the line was edited)",
                    screen_tail=screen_tail(st.lines),
                )
            now = time.monotonic()
            if now >= deadline:
                captured = self.capture_from(start_line)
                ext = extract_output(captured, nonce)
                out, omitted = truncate_lines(ext.lines, max_lines)
                return self._finish(
                    nonce,
                    "timeout",
                    RC_TIMEOUT,
                    out,
                    omitted,
                    cmd,
                    note=f"still running after {timeout:g}s; poll with `screen`, do not resend",
                    foreground=st.foreground,
                    screen_tail=screen_tail(st.lines),
                )
            time.sleep(min(delay, max(0.05, deadline - now)))
            delay = min(delay * POLL_FACTOR, POLL_MAX)

    @staticmethod
    def _finish(
        nonce: str,
        status: str,
        rc: int,
        output: list[str],
        omitted: int,
        cmd: str,
        note: str | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        res: dict[str, Any] = {
            "status": status,
            "rc": rc,
            "output": output,
            "omitted": omitted,
            "nonce": nonce,
            "command": cmd,
        }
        if note:
            res["note"] = note
        res.update(extra)
        log.info("exec[%s] %s rc=%s lines=%d", nonce, status, rc, len(output))
        return res


__all__ = [
    "RC_BUSY",
    "RC_NO_SENTINEL",
    "RC_TIMEOUT",
    "PaneState",
    "TmuxSession",
    "b64_shell_arg",
    "build_command_line",
    "compute_capture_start",
    "done_pattern",
    "extract_output",
    "find_done",
    "idle_reason",
    "is_shell",
    "literal_for_send_keys",
    "normalize_command",
    "parse_pane_state",
    "screen_tail",
    "sentinel_text",
    "shell_family",
    "shell_quote",
    "truncate_lines",
    "validate_exec_command",
    "validate_key_name",
    "validate_literal",
    "validate_session_name",
]
