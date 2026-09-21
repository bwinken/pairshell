"""Transports carry the invisible control channel to the remote host.

Everything above this package (tmux logic, exec/screen/keys) only sees the
:class:`~pairshell.transports.base.Transport` interface.
"""

from __future__ import annotations

from typing import Any

from .base import (
    AuthError,
    ConnectionLost,
    ControlTimeout,
    Transport,
    TransportError,
)


def make_transport(profile: Any, password: str | None = None) -> Transport:
    """Build the transport described by a profile object.

    ``profile`` needs ``protocol``, ``host``, ``port``, ``user`` and, for
    ssh, ``key_path``/``ssh_options`` attributes (a :class:`Profile`).
    """
    protocol = getattr(profile, "protocol", "")
    if protocol == "telnet":
        from .telnet import TelnetTransport

        return TelnetTransport(profile.host, int(profile.port or 23), profile.user, password or "")
    if protocol == "ssh":
        from .ssh import SshTransport

        return SshTransport(
            profile.host,
            int(profile.port or 22),
            profile.user,
            key_path=getattr(profile, "key_path", None) or None,
            ssh_options=list(getattr(profile, "ssh_options", None) or []),
        )
    if protocol == "local":
        from .local import LocalTransport

        return LocalTransport()
    raise TransportError(f"unknown protocol: {protocol!r}")


__all__ = [
    "AuthError",
    "ConnectionLost",
    "ControlTimeout",
    "Transport",
    "TransportError",
    "make_transport",
]
