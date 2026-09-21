"""Interactive profile menu (the default command).  Stdlib only.

Arrow keys / j k move, Enter attaches (sets ``current`` and starts serve in
the background), ``a`` add, ``e`` edit, ``d`` delete, ``s`` stop serve,
``r`` refresh, ``q`` quit.  Works inside the VS Code integrated terminal on
Windows (``msvcrt``) and on POSIX terminals (``termios``).
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime
from typing import Any

from . import __version__, credentials, dialogs
from .attach import AttachError, attach
from .profiles import Profile, ProfileStore, config_dir, get_current, set_current
from .serve import ServeError, ensure_running, obtain_password, probe_state, stop_serve
from .transports.base import TransportError

CLEAR = "\x1b[2J\x1b[H"
HOME = "\x1b[H"
ERASE_LINE = "\x1b[K"
ERASE_BELOW = "\x1b[J"


def render_frame(lines: list[str], full_clear: bool) -> str:
    """Repaint in place: home, overwrite each line, erase the rest.

    Clearing the whole screen on every keypress pushes a copy of the menu
    into the terminal's scrollback (Windows Terminal, VS Code) and flickers;
    overwriting does neither.  ``full_clear`` is used for the first frame and
    after another program (attach, a dialog) drew on the screen.
    """
    body = "".join(line + ERASE_LINE + "\r\n" for line in lines)
    return (CLEAR if full_clear else HOME) + body + ERASE_BELOW


# --------------------------------------------------------------------------
# Raw key input
# --------------------------------------------------------------------------


class _PosixKeys:
    def __init__(self) -> None:
        self.fd = sys.stdin.fileno()
        self._saved: Any = None

    def enter(self) -> None:
        import termios
        import tty

        self._saved = termios.tcgetattr(self.fd)
        tty.setcbreak(self.fd)

    def leave(self) -> None:
        if self._saved is not None:
            import termios

            termios.tcsetattr(self.fd, termios.TCSADRAIN, self._saved)
            self._saved = None

    def _read1(self, timeout: float | None) -> str | None:
        import select

        r, _, _ = select.select([self.fd], [], [], timeout)
        if not r:
            return None
        data = os.read(self.fd, 1)
        return data.decode("utf-8", "ignore") if data else None

    def read_key(self, timeout: float | None) -> str | None:
        ch = self._read1(timeout)
        if ch is None:
            return None
        if ch == "\x1b":
            seq = ""
            while len(seq) < 4:
                nxt = self._read1(0.05)
                if nxt is None:
                    break
                seq += nxt
                if seq[-1].isalpha() or seq[-1] == "~":
                    break
            return _decode_escape(seq)
        return _plain_key(ch)


class _WinKeys:  # pragma: no cover - Windows only
    def __init__(self) -> None:
        import msvcrt

        self.msvcrt = msvcrt
        self._saved_mode: Any = None

    def enter(self) -> None:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GetStdHandle.argtypes = [wintypes.DWORD]
        k32.GetStdHandle.restype = wintypes.HANDLE
        k32.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        k32.GetConsoleMode.restype = wintypes.BOOL
        k32.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k32.SetConsoleMode.restype = wintypes.BOOL
        hout = k32.GetStdHandle(wintypes.DWORD(-11 & 0xFFFFFFFF))
        mode = wintypes.DWORD()
        if k32.GetConsoleMode(hout, ctypes.byref(mode)):
            self._saved_mode = (k32, hout, mode)
            # ENABLE_VIRTUAL_TERMINAL_PROCESSING | ENABLE_PROCESSED_OUTPUT
            k32.SetConsoleMode(hout, mode.value | 0x0004 | 0x0001)

    def leave(self) -> None:
        if self._saved_mode is not None:
            k32, hout, mode = self._saved_mode
            k32.SetConsoleMode(hout, mode.value)
            self._saved_mode = None

    def read_key(self, timeout: float | None) -> str | None:
        m = self.msvcrt
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if m.kbhit():
                ch = m.getwch()
                if ch in ("\x00", "\xe0"):
                    code = m.getwch()
                    return {"H": "up", "P": "down", "K": "left", "M": "right", "G": "home", "O": "end"}.get(code, "")
                if ch == "\x1b":
                    seq = ""
                    t0 = time.monotonic()
                    while time.monotonic() - t0 < 0.05 and len(seq) < 4:
                        if m.kbhit():
                            seq += m.getwch()
                            if seq[-1].isalpha() or seq[-1] == "~":
                                break
                        else:
                            time.sleep(0.005)
                    return _decode_escape(seq)
                return _plain_key(ch)
            if deadline is not None and time.monotonic() >= deadline:
                return None
            time.sleep(0.03)


def _decode_escape(seq: str) -> str:
    if not seq:
        return "esc"
    table = {"[A": "up", "OA": "up", "[B": "down", "OB": "down", "[C": "right", "[D": "left", "[H": "home", "[F": "end"}
    return table.get(seq, "")


def _plain_key(ch: str) -> str:
    if ch in ("\r", "\n"):
        return "enter"
    if ch == "\x03":
        raise KeyboardInterrupt
    if ch in ("\x7f", "\x08"):
        return "backspace"
    return ch


def make_keys() -> Any:
    if sys.platform == "win32":
        return _WinKeys()
    return _PosixKeys()


# --------------------------------------------------------------------------
# Menu
# --------------------------------------------------------------------------


def _fmt_last_used(ts: float) -> str:
    if not ts:
        return "never"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


class Menu:
    def __init__(self) -> None:
        self.store = ProfileStore()
        self.profiles: list[Profile] = []
        self.states: dict[str, dict[str, Any]] = {}
        self.index = 0
        self.message = ""
        self.keys = make_keys()
        self._needs_clear = True

    # -- data ----------------------------------------------------------------

    def refresh(self, probe: bool = True) -> None:
        self.profiles = self.store.list()
        if self.profiles:
            self.index = max(0, min(self.index, len(self.profiles) - 1))
        else:
            self.index = 0
        if probe:
            self.states = {p.name: probe_state(p, timeout=1.0) for p in self.profiles}

    @property
    def selected(self) -> Profile | None:
        if not self.profiles:
            return None
        return self.profiles[self.index]

    # -- drawing -------------------------------------------------------------

    def render(self) -> None:
        current = get_current()
        lines = [f" pairshell {__version__} - profiles   (config: {config_dir()})", ""]
        if not self.profiles:
            lines.append("   No profiles yet. Press 'a' to add one.")
        else:
            w_name = max(4, max(len(p.name) for p in self.profiles))
            w_target = max(6, max(len(p.label) for p in self.profiles))
            lines.append(f"   {'NAME':<{w_name}}  {'PROTO':<6}  {'TARGET':<{w_target}}  {'SESSION':<12}  {'STATE':<12}  LAST USED")
            for i, p in enumerate(self.profiles):
                st = self.states.get(p.name, {})
                state = st.get("state", "stopped")
                if state == "busy" and st.get("foreground"):
                    state = f"busy ({st['foreground']})"
                marker = " > " if i == self.index else "   "
                cur = "  *current" if p.name == current else ""
                lines.append(
                    f"{marker}{p.name:<{w_name}}  {p.protocol:<6}  {p.label:<{w_target}}  {p.session:<12}  {state:<12}  {_fmt_last_used(p.last_used)}{cur}"
                )
        lines.append("")
        lines.append(" Up/Down move   Enter attach   a add   e edit   d delete   s stop serve   r refresh   q quit")
        if self.message:
            lines.append(" " + self.message)
        sys.stdout.write(render_frame(lines, self._needs_clear))
        sys.stdout.flush()
        self._needs_clear = False

    # -- actions -------------------------------------------------------------

    def _cooked(self) -> None:
        sys.stdout.write(CLEAR)  # while VT processing is still on
        sys.stdout.flush()
        self.keys.leave()

    def _raw(self) -> None:
        self.keys.enter()
        self._needs_clear = True  # something else drew on the screen meanwhile

    def _pause(self, text: str = "Press Enter to return to the menu...") -> None:
        try:
            input(text)
        except EOFError:
            pass

    def do_attach(self) -> None:
        p = self.selected
        if p is None:
            return
        self._cooked()
        try:
            set_current(p.name)
            self.store.touch(p.name)
            password = obtain_password(p, interactive=True) if p.needs_password else None
            if p.needs_password and not password:
                print(f"No password for {p.name}; store one with 'e' (edit).")
                self._pause()
                return
            try:
                ensure_running(p, announce=lambda m: print(m, flush=True))
            except ServeError as exc:
                print(f"[pairshell] warning: {exc}")
                if p.protocol == "telnet":
                    self._pause()
                    return
            rc = attach(p, password)
            if rc not in (0, None):
                print(f"[pairshell] attach exited with code {rc}")
                self._pause()
        except (AttachError, TransportError, OSError) as exc:
            print(f"[pairshell] {exc}")
            self._pause()
        finally:
            self._raw()
            self.refresh()

    def do_add(self) -> None:
        self._cooked()
        try:
            print("Add a profile (Ctrl-C to cancel)\n")
            prof = dialogs.prompt_profile()
            if self.store.exists(prof.name) and not dialogs.confirm(f"Profile {prof.name} exists; overwrite?"):
                return
            pw = dialogs.prompt_password(prof, existing=False)
            dialogs.save_profile(self.store, prof, pw)
            self.message = f"added {prof.name}"
        except KeyboardInterrupt:
            self.message = "cancelled"
        except (credentials.CredentialError, ValueError) as exc:
            print(f"[pairshell] {exc}")
            self._pause()
        finally:
            self._raw()
            self.refresh()

    def do_edit(self) -> None:
        p = self.selected
        if p is None:
            return
        self._cooked()
        try:
            print(f"Edit profile {p.name} (Ctrl-C to cancel)\n")
            prof = dialogs.prompt_profile(existing=p)
            pw = dialogs.prompt_password(prof, existing=credentials.has_password(prof.name))
            dialogs.save_profile(self.store, prof, pw)
            self.message = f"saved {prof.name}"
        except KeyboardInterrupt:
            self.message = "cancelled"
        except (credentials.CredentialError, ValueError) as exc:
            print(f"[pairshell] {exc}")
            self._pause()
        finally:
            self._raw()
            self.refresh()

    def do_delete(self) -> None:
        p = self.selected
        if p is None:
            return
        self._cooked()
        try:
            if dialogs.confirm(f"Delete profile {p.name} ({p.label})? The remote tmux session is not touched."):
                stop_serve(p.name)
                try:
                    credentials.delete_password(p.name)
                except credentials.CredentialError:
                    pass
                self.store.remove(p.name)
                self.message = f"deleted {p.name}"
        except KeyboardInterrupt:
            self.message = "cancelled"
        finally:
            self._raw()
            self.refresh()

    def do_stop(self) -> None:
        p = self.selected
        if p is None:
            return
        stopped = stop_serve(p.name)
        self.message = f"stopped serve for {p.name}" if stopped else f"serve for {p.name} was not running"
        self.refresh()

    # -- main loop -----------------------------------------------------------

    def run(self) -> int:
        if not sys.stdin.isatty():
            print("pairshell: the menu needs an interactive terminal; try `pairshell list`", file=sys.stderr)
            return 2
        self.refresh()
        self._raw()
        try:
            while True:
                self.render()
                try:
                    key = self.keys.read_key(3.0)
                except KeyboardInterrupt:
                    return 0
                if key is None:
                    self.refresh()
                    continue
                self.message = ""
                if key in ("q", "esc"):
                    return 0
                if key in ("up", "k") and self.profiles:
                    self.index = (self.index - 1) % len(self.profiles)
                elif key in ("down", "j") and self.profiles:
                    self.index = (self.index + 1) % len(self.profiles)
                elif key == "home":
                    self.index = 0
                elif key == "end" and self.profiles:
                    self.index = len(self.profiles) - 1
                elif key == "enter":
                    self.do_attach()
                elif key == "a":
                    self.do_add()
                elif key == "e":
                    self.do_edit()
                elif key == "d":
                    self.do_delete()
                elif key == "s":
                    self.do_stop()
                elif key == "r":
                    self.refresh()
                elif key and key.isdigit() and self.profiles:
                    n = int(key)
                    if 1 <= n <= len(self.profiles):
                        self.index = n - 1
        finally:
            sys.stdout.write(CLEAR)
            sys.stdout.flush()
            self.keys.leave()


def run_menu() -> int:
    return Menu().run()


__all__ = ["Menu", "run_menu"]
