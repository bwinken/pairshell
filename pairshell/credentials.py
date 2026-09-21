"""Password storage.  Never plaintext on disk, never printed.

* Windows: Credential Manager through ``ctypes`` (``CredWriteW`` /
  ``CredReadW`` / ``CredDeleteW``), target ``pairshell:<profile>``.
* macOS: the ``security`` keychain CLI (password passed as hex over stdin,
  never on the command line).
* Linux: ``secret-tool`` (libsecret) when installed.
* Otherwise nothing is stored; ``serve`` prompts on a terminal, or reads
  ``PAIRSHELL_PASSWORD`` from the environment.
"""

from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys

TARGET_PREFIX = "pairshell:"
ENV_PASSWORD = "PAIRSHELL_PASSWORD"


class CredentialError(Exception):
    pass


def target_name(profile: str) -> str:
    return TARGET_PREFIX + profile


def backend() -> str:
    if sys.platform == "win32":
        return "windows-credential-manager"
    if sys.platform == "darwin" and shutil.which("security"):
        return "macos-keychain"
    if shutil.which("secret-tool"):
        return "secret-tool"
    return "none"


# --------------------------------------------------------------------------
# Windows Credential Manager
# --------------------------------------------------------------------------

if sys.platform == "win32":  # pragma: no cover - exercised only on Windows
    from ctypes import wintypes

    CRED_TYPE_GENERIC = 1
    CRED_PERSIST_LOCAL_MACHINE = 2
    ERROR_NOT_FOUND = 1168

    class _FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

    class _CREDENTIAL_ATTRIBUTE(ctypes.Structure):
        _fields_ = [
            ("Keyword", wintypes.LPWSTR),
            ("Flags", wintypes.DWORD),
            ("ValueSize", wintypes.DWORD),
            ("Value", wintypes.LPBYTE),
        ]

    class _CREDENTIAL(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", _FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", wintypes.LPBYTE),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.POINTER(_CREDENTIAL_ATTRIBUTE)),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    _PCREDENTIAL = ctypes.POINTER(_CREDENTIAL)
    _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    _advapi32.CredWriteW.argtypes = [_PCREDENTIAL, wintypes.DWORD]
    _advapi32.CredWriteW.restype = wintypes.BOOL
    _advapi32.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(_PCREDENTIAL)]
    _advapi32.CredReadW.restype = wintypes.BOOL
    _advapi32.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    _advapi32.CredDeleteW.restype = wintypes.BOOL
    _advapi32.CredFree.argtypes = [wintypes.LPVOID]
    _advapi32.CredFree.restype = None

    def _win_store(profile: str, username: str, password: str) -> None:
        blob = password.encode("utf-16-le")
        buf = ctypes.create_string_buffer(blob, len(blob))
        cred = _CREDENTIAL()
        cred.Flags = 0
        cred.Type = CRED_TYPE_GENERIC
        cred.TargetName = target_name(profile)
        cred.Comment = "pairshell remote login password"
        cred.CredentialBlobSize = len(blob)
        cred.CredentialBlob = ctypes.cast(buf, wintypes.LPBYTE)
        cred.Persist = CRED_PERSIST_LOCAL_MACHINE
        cred.AttributeCount = 0
        cred.Attributes = None
        cred.TargetAlias = None
        cred.UserName = username or None
        if not _advapi32.CredWriteW(ctypes.byref(cred), 0):
            raise CredentialError(f"CredWriteW failed (error {ctypes.get_last_error()})")

    def _win_load(profile: str) -> str | None:
        pcred = _PCREDENTIAL()
        if not _advapi32.CredReadW(target_name(profile), CRED_TYPE_GENERIC, 0, ctypes.byref(pcred)):
            err = ctypes.get_last_error()
            if err == ERROR_NOT_FOUND:
                return None
            raise CredentialError(f"CredReadW failed (error {err})")
        try:
            c = pcred.contents
            blob = ctypes.string_at(c.CredentialBlob, c.CredentialBlobSize) if c.CredentialBlobSize else b""
        finally:
            _advapi32.CredFree(pcred)
        try:
            return blob.decode("utf-16-le")
        except UnicodeDecodeError:
            return blob.decode("utf-8", "replace")

    def _win_delete(profile: str) -> None:
        if not _advapi32.CredDeleteW(target_name(profile), CRED_TYPE_GENERIC, 0):
            err = ctypes.get_last_error()
            if err != ERROR_NOT_FOUND:
                raise CredentialError(f"CredDeleteW failed (error {err})")


# --------------------------------------------------------------------------
# macOS keychain / libsecret
# --------------------------------------------------------------------------


def _run(argv: list[str], input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, input=input_text, capture_output=True, text=True, check=False)


def _mac_store(profile: str, username: str, password: str) -> None:
    hexpw = password.encode("utf-8").hex()
    script = f"add-generic-password -U -a {username or 'pairshell'} -s {target_name(profile)} -X {hexpw}\n"
    r = _run(["security", "-i"], script)
    if r.returncode != 0:
        raise CredentialError("keychain write failed")


def _mac_load(profile: str) -> str | None:
    r = _run(["security", "find-generic-password", "-s", target_name(profile), "-w"])
    if r.returncode != 0:
        return None
    return r.stdout.rstrip("\n")


def _mac_delete(profile: str) -> None:
    _run(["security", "delete-generic-password", "-s", target_name(profile)])


def _st_store(profile: str, username: str, password: str) -> None:
    r = _run(
        ["secret-tool", "store", f"--label=pairshell {profile}", "service", "pairshell", "profile", profile],
        password,
    )
    if r.returncode != 0:
        raise CredentialError("secret-tool store failed (is a keyring daemon running?)")


def _st_load(profile: str) -> str | None:
    r = _run(["secret-tool", "lookup", "service", "pairshell", "profile", profile])
    if r.returncode != 0 or not r.stdout:
        return None
    return r.stdout


def _st_delete(profile: str) -> None:
    _run(["secret-tool", "clear", "service", "pairshell", "profile", profile])


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------


def store_password(profile: str, username: str, password: str) -> None:
    b = backend()
    if b == "windows-credential-manager":
        _win_store(profile, username, password)  # type: ignore[name-defined]
    elif b == "macos-keychain":
        _mac_store(profile, username, password)
    elif b == "secret-tool":
        _st_store(profile, username, password)
    else:
        raise CredentialError(
            "no credential store available on this platform; set PAIRSHELL_PASSWORD "
            "in the environment of `pairshell serve`, or run serve in a terminal to be prompted"
        )


def load_password(profile: str) -> str | None:
    b = backend()
    if b == "windows-credential-manager":
        return _win_load(profile)  # type: ignore[name-defined]
    if b == "macos-keychain":
        return _mac_load(profile)
    if b == "secret-tool":
        return _st_load(profile)
    return None


def delete_password(profile: str) -> None:
    b = backend()
    if b == "windows-credential-manager":
        _win_delete(profile)  # type: ignore[name-defined]
    elif b == "macos-keychain":
        _mac_delete(profile)
    elif b == "secret-tool":
        _st_delete(profile)


def has_password(profile: str) -> bool:
    try:
        return load_password(profile) is not None
    except CredentialError:
        return False


def resolve_password(profile: str) -> str | None:
    """Environment override first, then the platform store."""
    env = os.environ.get(ENV_PASSWORD)
    if env:
        return env
    try:
        return load_password(profile)
    except CredentialError:
        return None


__all__ = [
    "ENV_PASSWORD",
    "CredentialError",
    "backend",
    "delete_password",
    "has_password",
    "load_password",
    "resolve_password",
    "store_password",
    "target_name",
]
