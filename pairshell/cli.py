"""Command-line interface.  ``pairshell --help`` lists everything.

Exit codes: 0 ok, 2 pairshell/usage error, 3 pane busy (nothing sent),
124 command still running after the timeout, 125 the shell came back to a
prompt without the sentinel; otherwise the remote command's exit code.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from typing import Any

from . import __version__, credentials, dialogs, rpc
from .attach import AttachError, attach
from .profiles import (
    PROTOCOLS,
    Profile,
    ProfileError,
    ProfileNotFound,
    ProfileStore,
    RunState,
    clear_current,
    config_dir,
    get_current,
    set_current,
)
from .serve import ServeError, ensure_running, log_tail, obtain_password, probe_state, run_serve, serve_state, stop_serve
from .tmuxops import validate_exec_command, validate_key_name, validate_literal
from .transports.base import TransportError


def err(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _force_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def resolve_profile(store: ProfileStore, name: str | None) -> Profile:
    if name:
        return store.get(name)
    cur = get_current()
    if cur and store.exists(cur):
        return store.get(cur)
    names = store.names()
    if len(names) == 1:
        return store.get(names[0])
    if not names:
        raise ProfileError("no profiles yet: run `pairshell add` (or `pairshell` for the menu)")
    raise ProfileError(f"no current profile: pass --to <profile> or pick one in the menu; profiles: {', '.join(names)}")


def connect(profile: Profile) -> RunState:
    try:
        return ensure_running(profile, announce=err)
    except ServeError as exc:
        raise ServeError(f"{exc}\nStart it manually with:  pairshell serve {profile.name}") from None


def call(state: RunState, op: str, args: dict[str, Any] | None = None, timeout: float = 60.0) -> Any:
    return rpc.call(state.rpc_port, state.token, op, args or {}, timeout=timeout)


def _print_lines(lines: list[str]) -> None:
    out = sys.stdout
    for line in lines:
        out.write(line + "\n")
    out.flush()


def print_exec_result(res: dict[str, Any]) -> None:
    status = res.get("status")
    if status == "busy":
        err(f"pairshell: pane busy (rc 3): {res.get('reason')}. Nothing was sent.")
        tail = res.get("screen_tail") or []
        if tail:
            err("--- screen tail ---")
            for line in tail:
                err(line)
            err("-------------------")
        err("Check `pairshell screen`, wait for the user, or pass --force if you are sure.")
        return
    if res.get("omitted"):
        print(f"[pairshell: {res['omitted']} earlier lines omitted; use --max-lines or redirect to a file]")
    _print_lines(res.get("output") or [])
    if status == "timeout":
        err(f"pairshell: rc 124 - {res.get('note')} (foreground: {res.get('foreground')})")
    elif status == "no_sentinel":
        err(f"pairshell: rc 125 - {res.get('note')}")
        tail = res.get("screen_tail") or []
        if tail:
            err("--- screen tail ---")
            for line in tail:
                err(line)
            err("-------------------")
    elif status == "no_session":
        err(f"pairshell: rc 125 - {res.get('note')}")


def _screen_header(profile: Profile, res: dict[str, Any]) -> str:
    state = "idle" if res.get("idle") else f"busy: {res.get('busy_reason')}"
    return (
        f"[pairshell] {profile.name} | foreground: {res.get('foreground')} | {state} | "
        f"window {res.get('window')} | {res.get('attached_clients', 0)} client(s) attached"
    )


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------


def cmd_menu(_args: argparse.Namespace) -> int:
    from .menu import run_menu

    return run_menu()


def cmd_attach(args: argparse.Namespace) -> int:
    store = ProfileStore()
    profile = store.get(args.profile)
    set_current(profile.name)
    store.touch(profile.name)
    password = obtain_password(profile, interactive=True) if profile.needs_password else None
    if profile.needs_password and not password:
        raise ProfileError(f"no password for {profile.name}: store one with `pairshell edit {profile.name}`")
    try:
        ensure_running(profile, announce=err)
    except ServeError as exc:
        err(f"[pairshell] warning: {exc}")
        if profile.protocol == "telnet":
            return 2
    return attach(profile, password)


def cmd_serve(args: argparse.Namespace) -> int:
    return run_serve(args.profile, foreground=not args.background)


def cmd_stop(args: argparse.Namespace) -> int:
    store = ProfileStore()
    profile = store.get(args.profile)
    if stop_serve(profile.name):
        err(f"[pairshell] stopped serve for {profile.name} (the remote tmux session '{profile.session}' keeps running)")
    else:
        err(f"[pairshell] serve for {profile.name} was not running")
    return 0


def cmd_exec(args: argparse.Namespace) -> int:
    for cmd in args.commands:
        validate_exec_command(cmd)
    store = ProfileStore()
    profile = resolve_profile(store, args.to)
    state = connect(profile)
    multi = len(args.commands) > 1
    results: list[dict[str, Any]] = []
    exit_code = 0
    for cmd in args.commands:
        if multi and not args.json:
            print(f"### {cmd}", flush=True)
        res = call(
            state,
            "exec",
            {"cmd": cmd, "timeout": args.timeout, "force": args.force, "max_lines": args.max_lines},
            timeout=args.timeout + 90,
        )
        res["profile"] = profile.name
        results.append(res)
        rc = int(res.get("rc", 2))
        if not args.json:
            print_exec_result(res)
            if multi:
                print(f"### rc={rc}", flush=True)
        if rc != 0:
            exit_code = rc
        if res.get("status") != "done":
            # busy (3), timeout (124), no sentinel / no session (125): later
            # commands would land on top of the problem, so stop here.
            break
    if args.json:
        print(json.dumps(results if multi else results[0], indent=2, ensure_ascii=False))
    return exit_code


def cmd_screen(args: argparse.Namespace) -> int:
    store = ProfileStore()
    profile = resolve_profile(store, args.to)
    state = connect(profile)
    res = call(state, "screen", {"lines": args.lines})
    if args.json:
        res["profile"] = profile.name
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0
    err(_screen_header(profile, res))
    _print_lines(res.get("lines") or [])
    return 0


def parse_key_items(tokens: list[str]) -> tuple[list[tuple[str, str]], str | None]:
    items: list[tuple[str, str]] = []
    target: str | None = None
    it = iter(tokens)
    for tok in it:
        if tok == "--literal":
            text = next(it, None)
            if text is None:
                raise ValueError("--literal needs a text argument")
            items.append(("literal", validate_literal(text)))
        elif tok.startswith("--literal="):
            items.append(("literal", validate_literal(tok[len("--literal=") :])))
        elif tok == "--to":
            target = next(it, None)
            if target is None:
                raise ValueError("--to needs a profile name")
        elif tok.startswith("--to="):
            target = tok[len("--to=") :]
        else:
            items.append(("key", validate_key_name(tok)))
    if not items:
        raise ValueError("no keys given (examples: C-c | q Enter | --literal ':wq' Enter)")
    return items, target


def cmd_keys(args: argparse.Namespace) -> int:
    items, target = parse_key_items(list(args.items))
    store = ProfileStore()
    profile = resolve_profile(store, target or args.to)
    state = connect(profile)
    res = call(state, "keys", {"items": items})
    if args.json:
        res["profile"] = profile.name
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0
    err(_screen_header(profile, res))
    _print_lines(res.get("lines") or [])
    return 0


def _fmt_uptime(started: float | None) -> str:
    if not started:
        return "?"
    secs = int(time.time() - started)
    if secs < 90:
        return f"{secs}s"
    if secs < 5400:
        return f"{secs // 60}m"
    return f"{secs // 3600}h{(secs % 3600) // 60:02d}m"


def cmd_status(args: argparse.Namespace) -> int:
    store = ProfileStore()
    profile = resolve_profile(store, args.to)
    base = serve_state(profile.name)
    info: dict[str, Any] = {
        "profile": profile.name,
        "protocol": profile.protocol,
        "target": profile.label,
        "session": profile.session,
        "serve": {"running": base["running"], "pid": base["pid"], "rpc_port": base["rpc_port"] or profile.rpc_port, "started_at": base["started_at"]},
    }
    if base["running"]:
        state = RunState(pid=base["pid"], rpc_port=base["rpc_port"], token=base["token"], started_at=base["started_at"] or 0.0, profile=profile.name)
        try:
            info.update(call(state, "status", timeout=10.0))
        except rpc.RpcError as exc:
            info["ok"] = False
            info["error"] = str(exc)
    else:
        info["ok"] = False
        info["connected"] = False
    if args.json:
        print(json.dumps(info, indent=2, ensure_ascii=False))
        return 0
    print(f"profile:    {profile.name} ({profile.protocol} {profile.label})")
    s = info["serve"]
    if s["running"]:
        print(f"serve:      running (pid {s['pid']}, rpc 127.0.0.1:{s['rpc_port']}, up {_fmt_uptime(s['started_at'])})")
    else:
        print(f"serve:      stopped (any `pairshell exec`/`attach` starts it; log: {log_tail(profile.name, 1) or 'none'})")
    print(f"connected:  {'yes' if info.get('connected') else 'no'}")
    if info.get("ok"):
        print(f"session:    {info.get('session')}, window {info.get('window')}, {info.get('size')}, {info.get('attached_clients')} client(s) attached")
        fam = info.get("shell_family")
        print(f"foreground: {info.get('foreground')}" + (f" ({fam} family)" if fam else ""))
        print(f"idle:       {'yes' if info.get('idle') else 'no - ' + str(info.get('busy_reason'))}")
        print(f"cursor:     {info.get('cursor_line')!r}")
    elif info.get("error"):
        print(f"error:      {info['error']}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    store = ProfileStore()
    current = get_current()
    rows: list[dict[str, Any]] = []
    for p in store.list():
        st = probe_state(p) if not args.no_probe else serve_state(p.name) | {"state": "?"}
        rows.append(
            {
                "name": p.name,
                "protocol": p.protocol,
                "host": p.host,
                "port": p.port,
                "user": p.user,
                "target": p.label,
                "session": p.session,
                "rpc_port": p.rpc_port,
                "key_path": p.key_path,
                "last_used": p.last_used,
                "current": p.name == current,
                "running": bool(st.get("running")),
                "pid": st.get("pid"),
                "state": st.get("state", "stopped"),
                "detail": st.get("detail", ""),
                "foreground": st.get("foreground"),
            }
        )
    if args.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0
    if not rows:
        print("no profiles yet: `pairshell add` or `pairshell` for the menu")
        return 0
    w_name = max(4, max(len(r["name"]) for r in rows))
    w_target = max(6, max(len(r["target"]) for r in rows))
    print(f"  {'NAME':<{w_name}}  {'PROTO':<6}  {'TARGET':<{w_target}}  {'SESSION':<12}  {'STATE':<10}  RPC    LAST USED")
    for r in rows:
        mark = "*" if r["current"] else " "
        last = datetime.fromtimestamp(r["last_used"]).strftime("%Y-%m-%d %H:%M") if r["last_used"] else "never"
        print(f"{mark} {r['name']:<{w_name}}  {r['protocol']:<6}  {r['target']:<{w_target}}  {r['session']:<12}  {r['state']:<10}  {r['rpc_port']:<5}  {last}")
    print(f"\n* = current target for exec/screen/keys (config: {config_dir()})")
    return 0


def _profile_from_flags(args: argparse.Namespace, existing: Profile | None, name: str) -> Profile:
    e = existing
    protocol = args.protocol or (e.protocol if e else "ssh")
    prof = Profile(
        name=name,
        protocol=protocol,
        host=args.host or (e.host if e else ""),
        port=int(args.port) if args.port else (e.port if e and e.protocol == protocol else 0),
        user=args.user or (e.user if e else ""),
        key_path=args.key if args.key is not None else (e.key_path if e else ""),
        session=args.session or (e.session if e else ""),
        rpc_port=e.rpc_port if e else 0,
        last_used=e.last_used if e else 0.0,
        ssh_options=args.ssh_option if args.ssh_option else (list(e.ssh_options) if e else []),
    )
    return prof.validate()


def _read_password_stdin() -> str:
    data = sys.stdin.readline()
    return data.rstrip("\r\n")


def _flags_given(args: argparse.Namespace) -> bool:
    return any(getattr(args, k) not in (None, [], False) for k in ("protocol", "host", "port", "user", "key", "session", "ssh_option", "password_stdin"))


def cmd_add(args: argparse.Namespace) -> int:
    store = ProfileStore()
    if _flags_given(args):
        if not args.name:
            raise ProfileError("a profile name is required")
        if store.exists(args.name) and not args.force:
            raise ProfileError(f"profile {args.name} exists; use `pairshell edit {args.name}` or --force")
        prof = _profile_from_flags(args, None, args.name)
        pw = _read_password_stdin() if args.password_stdin else None
        if prof.needs_password and pw is None:
            err(f"[pairshell] note: no password stored for {prof.name}; add one with --password-stdin or `pairshell edit {prof.name}`")
        dialogs.save_profile(store, prof, pw)
        err(f"[pairshell] saved profile {prof.name} ({prof.protocol} {prof.label}); tmux session '{prof.session}', rpc port {prof.rpc_port}")
        return 0
    if not sys.stdin.isatty():
        raise ProfileError("no terminal: pass --host/--user/... flags to add a profile non-interactively")
    print("Add a profile (Ctrl-C to cancel)\n")
    prof = dialogs.prompt_profile(name=args.name)
    if store.exists(prof.name) and not dialogs.confirm(f"Profile {prof.name} exists; overwrite?"):
        return 1
    pw = dialogs.prompt_password(prof, existing=False)
    dialogs.save_profile(store, prof, pw)
    print(f"\nSaved {prof.name}. Attach with `pairshell attach {prof.name}` or run `pairshell` for the menu.")
    return 0


def cmd_edit(args: argparse.Namespace) -> int:
    store = ProfileStore()
    existing = store.get(args.name)
    if _flags_given(args):
        prof = _profile_from_flags(args, existing, existing.name)
        pw = _read_password_stdin() if args.password_stdin else None
        dialogs.save_profile(store, prof, pw)
        err(f"[pairshell] saved profile {prof.name}")
        if serve_state(prof.name)["running"]:
            err(f"[pairshell] note: serve for {prof.name} is running with the old settings; `pairshell stop {prof.name}` to apply")
        return 0
    if not sys.stdin.isatty():
        raise ProfileError("no terminal: pass flags to edit non-interactively")
    print(f"Edit profile {existing.name} (Ctrl-C to cancel)\n")
    prof = dialogs.prompt_profile(existing=existing)
    pw = dialogs.prompt_password(prof, existing=credentials.has_password(prof.name))
    dialogs.save_profile(store, prof, pw)
    print(f"\nSaved {prof.name}.")
    if serve_state(prof.name)["running"]:
        print(f"serve for {prof.name} is running with the old settings; `pairshell stop {prof.name}` to apply.")
    return 0


def cmd_rm(args: argparse.Namespace) -> int:
    store = ProfileStore()
    prof = store.get(args.name)
    if not args.yes:
        if not sys.stdin.isatty():
            raise ProfileError("pass -y to delete without confirmation")
        if not dialogs.confirm(f"Delete profile {prof.name} ({prof.label})? The remote tmux session is not touched."):
            return 1
    stop_serve(prof.name)
    try:
        credentials.delete_password(prof.name)
    except credentials.CredentialError as exc:
        err(f"[pairshell] warning: {exc}")
    store.remove(prof.name)
    err(f"[pairshell] removed profile {prof.name}")
    return 0


def cmd_ctl(args: argparse.Namespace) -> int:
    store = ProfileStore()
    profile = resolve_profile(store, args.to)
    state = connect(profile)
    res = call(state, "ctl", {"cmd": args.cmd, "timeout": args.timeout}, timeout=args.timeout + 30)
    sys.stdout.write(res.get("output", ""))
    if res.get("output") and not str(res["output"]).endswith("\n"):
        sys.stdout.write("\n")
    sys.stdout.flush()
    return int(res.get("rc", 0))


def cmd_current(args: argparse.Namespace) -> int:
    store = ProfileStore()
    if args.clear:
        clear_current()
        return 0
    if args.name:
        prof = store.get(args.name)
        set_current(prof.name)
        err(f"[pairshell] current = {prof.name}")
        return 0
    cur = get_current()
    print(cur or "")
    return 0 if cur else 1


# --------------------------------------------------------------------------
# parser
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pairshell",
        description="Pair-program in a remote shell with your AI agent: you and the agent share one tmux session.",
        epilog="Run without arguments for the interactive menu.",
    )
    p.add_argument("--version", action="version", version=f"pairshell {__version__}")
    sub = p.add_subparsers(dest="command", metavar="command")

    def add_to(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--to", metavar="PROFILE", help="target profile (default: the current one)")

    sp = sub.add_parser("menu", help="interactive profile menu (default)")
    sp.set_defaults(func=cmd_menu)

    sp = sub.add_parser("attach", help="start serve if needed and attach this terminal to the tmux session")
    sp.add_argument("profile")
    sp.set_defaults(func=cmd_attach)

    sp = sub.add_parser("serve", help="hold the control channel in the foreground (normally started for you)")
    sp.add_argument("profile")
    sp.add_argument("--background", action="store_true", help=argparse.SUPPRESS)
    sp.set_defaults(func=cmd_serve)

    sp = sub.add_parser("stop", help="stop the serve process (the remote tmux session survives)")
    sp.add_argument("profile")
    sp.set_defaults(func=cmd_stop)

    sp = sub.add_parser("exec", help="run command(s) in the shared pane and return their output and exit code")
    add_to(sp)
    sp.add_argument("commands", nargs="+", metavar="cmd", help="one command line per argument")
    sp.add_argument("--timeout", type=float, default=120.0, help="seconds to wait (default 120); rc 124 when exceeded")
    sp.add_argument("--force", action="store_true", help="send even if the pane does not look idle")
    sp.add_argument("--max-lines", type=int, default=500, help="keep only the last N output lines (default 500)")
    sp.add_argument("--json", action="store_true", help="machine-readable result")
    sp.set_defaults(func=cmd_exec)

    sp = sub.add_parser("screen", help="print the visible pane (plus N lines of scrollback)")
    add_to(sp)
    sp.add_argument("-n", "--lines", type=int, default=0, metavar="N", help="scrollback lines to include")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_screen)

    sp = sub.add_parser(
        "keys",
        help="send keys (C-c, Enter, q ...) or --literal TEXT, then print the screen",
        usage="pairshell keys [--to PROFILE] [--json] KEY... [--literal TEXT] ...",
    )
    add_to(sp)
    sp.add_argument("--json", action="store_true")
    sp.add_argument("items", nargs=argparse.REMAINDER, help="key names; free text via --literal TEXT")
    sp.set_defaults(func=cmd_keys)

    sp = sub.add_parser("status", help="foreground process, idle state, attached clients, serve state")
    add_to(sp)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("list", help="profiles and their live state")
    sp.add_argument("--json", action="store_true")
    sp.add_argument("--no-probe", action="store_true", help="do not query running serve processes")
    sp.set_defaults(func=cmd_list)

    for name, help_text in (("add", "add a profile"), ("edit", "edit a profile")):
        sp = sub.add_parser(name, help=help_text + " (interactive, or with flags)")
        sp.add_argument("name", nargs="?" if name == "add" else None)
        sp.add_argument("--protocol", choices=PROTOCOLS)
        sp.add_argument("--host")
        sp.add_argument("--port", type=int)
        sp.add_argument("--user")
        sp.add_argument("--key", metavar="PATH", help="ssh private key")
        sp.add_argument("--session", help="tmux session name (default: profile name)")
        sp.add_argument("--ssh-option", action="append", metavar="ARG", help="extra ssh argument (repeatable, e.g. --ssh-option=-oProxyJump=bastion)")
        sp.add_argument("--password-stdin", action="store_true", help="read the telnet password from the first line of stdin")
        if name == "add":
            sp.add_argument("--force", action="store_true", help="overwrite an existing profile")
            sp.set_defaults(func=cmd_add)
        else:
            sp.set_defaults(func=cmd_edit)

    sp = sub.add_parser("rm", help="remove a profile and its stored password")
    sp.add_argument("name")
    sp.add_argument("-y", "--yes", action="store_true")
    sp.set_defaults(func=cmd_rm)

    sp = sub.add_parser("ctl", help="run a raw command in the invisible control shell (diagnostics only)")
    add_to(sp)
    sp.add_argument("cmd")
    sp.add_argument("--timeout", type=float, default=30.0)
    sp.set_defaults(func=cmd_ctl)

    sp = sub.add_parser("current", help="show or set the default target profile")
    sp.add_argument("name", nargs="?")
    sp.add_argument("--clear", action="store_true")
    sp.set_defaults(func=cmd_current)
    return p


def _split_keys_argv(argv: list[str]) -> list[str]:
    """Keep argparse away from the free-form part of ``keys``.

    Everything after ``keys`` that is not ``--to X``/``--json`` is handed to
    :func:`parse_key_items` verbatim, so ``--literal`` may come first.
    """
    if not argv or argv[0] != "keys":
        return argv
    head: list[str] = ["keys"]
    rest: list[str] = []
    it = iter(argv[1:])
    for tok in it:
        if tok == "--to":
            head += [tok, next(it, "")]
        elif tok.startswith("--to=") or tok == "--json":
            head.append(tok)
        elif tok in ("-h", "--help") and not rest:
            head.append(tok)
        else:
            rest.append(tok)
    return head + ["--"] + rest if rest else head


def main(argv: list[str] | None = None) -> int:
    _force_utf8()
    parser = build_parser()
    argv = list(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(_split_keys_argv(argv))
    if not getattr(args, "command", None):
        return cmd_menu(args)
    try:
        return int(args.func(args) or 0)
    except ProfileNotFound as exc:
        err(f"pairshell: {exc}")
        return 2
    except (ProfileError, ServeError, rpc.RpcError, TransportError, AttachError, credentials.CredentialError, ValueError) as exc:
        err(f"pairshell: {exc}")
        return 2
    except KeyboardInterrupt:
        err("")
        return 130
    except BrokenPipeError:
        return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
