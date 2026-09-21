"""Interactive prompts shared by the CLI (``add``/``edit``) and the menu."""

from __future__ import annotations

import getpass
import sys

from . import credentials
from .profiles import PROTOCOLS, Profile, ProfileError, ProfileStore, sanitize_session_name


def ask(prompt: str, default: str | None = None, required: bool = False) -> str:
    while True:
        suffix = f" [{default}]" if default not in (None, "") else ""
        try:
            value = input(f"{prompt}{suffix}: ").strip()
        except EOFError:
            value = ""
        if not value and default is not None:
            return default
        if value or not required:
            return value
        print("  (required)")


def confirm(prompt: str, default: bool = False) -> bool:
    hint = "Y/n" if default else "y/N"
    try:
        value = input(f"{prompt} [{hint}] ").strip().lower()
    except EOFError:
        return default
    if not value:
        return default
    return value in ("y", "yes")


def prompt_profile(existing: Profile | None = None, name: str | None = None) -> Profile:
    """Ask for every profile field, offering the existing values as defaults."""
    e = existing
    while True:
        pname = e.name if e else ask("Profile name", name, required=True)
        protocols = "/".join(PROTOCOLS) if sys.platform != "win32" else "telnet/ssh"
        protocol = ask(f"Protocol ({protocols})", e.protocol if e else "ssh").lower()
        if protocol not in PROTOCOLS:
            print(f"  protocol must be one of {protocols}")
            continue
        host = user = key_path = ""
        port = 0
        ssh_options = list(e.ssh_options) if e else []
        if protocol != "local":
            host = ask("Host", e.host if e else None, required=True)
            default_port = str(e.port) if e and e.port else ("23" if protocol == "telnet" else "22")
            port_s = ask("Port", default_port)
            try:
                port = int(port_s)
            except ValueError:
                print("  port must be a number")
                continue
            user = ask("User", e.user if e else getpass.getuser(), required=True)
            if protocol == "ssh":
                key_path = ask("Private key path (empty = ssh defaults/agent)", e.key_path if e else "")
        session = ask("tmux session name", e.session if e else sanitize_session_name(pname))
        prompt_regex = ask("Prompt regex (empty = default, ends with % $ # >)", e.prompt_regex if e else "")
        prof = Profile(
            name=pname,
            protocol=protocol,
            host=host,
            port=port,
            user=user,
            key_path=key_path,
            session=session,
            rpc_port=e.rpc_port if e else 0,
            last_used=e.last_used if e else 0.0,
            ssh_options=ssh_options,
            prompt_regex=prompt_regex,
        )
        try:
            prof.validate()
        except ProfileError as exc:
            print(f"  {exc}")
            continue
        return prof


def prompt_password(profile: Profile, existing: bool) -> str | None:
    """Ask for a telnet password; returns None to keep the stored one."""
    if not profile.needs_password:
        return None
    b = credentials.backend()
    if b == "none":
        print(
            "  No credential store on this platform: the password will not be saved.\n"
            f"  Run `pairshell serve {profile.name}` in a terminal to be prompted, or set PAIRSHELL_PASSWORD."
        )
        return None
    label = "Password (leave empty to keep the stored one)" if existing else "Password"
    while True:
        pw = getpass.getpass(f"{label}: ")
        if not pw and existing:
            return None
        if not pw:
            print("  (required for telnet profiles)")
            continue
        again = getpass.getpass("Repeat password: ")
        if pw != again:
            print("  passwords do not match")
            continue
        return pw


def save_profile(store: ProfileStore, profile: Profile, password: str | None) -> Profile:
    saved = store.upsert(profile)
    if password is not None:
        credentials.store_password(saved.name, saved.user, password)
    return saved


__all__ = ["ask", "confirm", "prompt_password", "prompt_profile", "save_profile"]
