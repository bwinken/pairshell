"""Tiny JSON-line RPC between the ``pairshell`` CLI and a ``serve`` process.

One request per TCP connection on ``127.0.0.1:<rpc_port>``::

    -> {"token": "...", "op": "exec", "args": {...}}\\n
    <- {"ok": true, "result": {...}}\\n       or
    <- {"ok": false, "error": "...", "kind": "ExceptionName"}\\n

The token is generated per serve process and stored in the run-state file,
so only the same user account can drive the session.
"""

from __future__ import annotations

import json
import logging
import socket
import socketserver
import sys
import threading
from typing import Any, Callable

log = logging.getLogger("pairshell.rpc")

MAX_REQUEST = 16 * 1024 * 1024
DEFAULT_CONNECT_TIMEOUT = 3.0


class RpcError(Exception):
    """Could not reach the serve process or it answered garbage."""


class RpcRemoteError(RpcError):
    """The serve process reported an error while handling the request."""

    def __init__(self, message: str, kind: str | None = None) -> None:
        super().__init__(message)
        self.kind = kind or "Error"


def _recv_line(sock: socket.socket, limit: int = MAX_REQUEST) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if chunk.endswith(b"\n"):
            break
        if total > limit:
            raise RpcError("RPC message too large")
    return b"".join(chunks)


def call(
    port: int,
    token: str,
    op: str,
    args: dict[str, Any] | None = None,
    timeout: float = 30.0,
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
) -> Any:
    """Send one request and return the ``result`` field."""
    try:
        sock = socket.create_connection(("127.0.0.1", int(port)), timeout=connect_timeout)
    except OSError as exc:
        raise RpcError(f"cannot connect to serve on 127.0.0.1:{port}: {exc}") from exc
    try:
        sock.settimeout(timeout)
        payload = json.dumps({"token": token, "op": op, "args": args or {}}, ensure_ascii=False) + "\n"
        sock.sendall(payload.encode("utf-8"))
        try:
            sock.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        raw = _recv_line(sock)
    except socket.timeout as exc:
        raise RpcError(f"serve did not answer within {timeout:g}s") from exc
    except OSError as exc:
        raise RpcError(f"RPC failed: {exc}") from exc
    finally:
        sock.close()
    if not raw:
        raise RpcError("serve closed the connection without answering")
    try:
        resp = json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        raise RpcError(f"malformed RPC response: {raw[:200]!r}") from exc
    if not isinstance(resp, dict):
        raise RpcError("malformed RPC response")
    if not resp.get("ok"):
        raise RpcRemoteError(str(resp.get("error", "unknown error")), resp.get("kind"))
    return resp.get("result")


Handler = Callable[[str, dict[str, Any]], Any]


class _RequestHandler(socketserver.StreamRequestHandler):
    timeout = 3600

    def handle(self) -> None:  # noqa: D401 - socketserver API
        server: RpcServer = self.server  # type: ignore[assignment]
        try:
            raw = self.rfile.readline(MAX_REQUEST)
        except OSError:
            return
        if not raw:
            return
        try:
            req = json.loads(raw.decode("utf-8"))
            if not isinstance(req, dict):
                raise ValueError("request is not an object")
        except ValueError as exc:
            self._reply({"ok": False, "error": f"bad request: {exc}", "kind": "BadRequest"})
            return
        if req.get("token") != server.token:
            self._reply({"ok": False, "error": "bad token", "kind": "Unauthorized"})
            return
        op = str(req.get("op", ""))
        args = req.get("args") or {}
        if not isinstance(args, dict):
            self._reply({"ok": False, "error": "args must be an object", "kind": "BadRequest"})
            return
        try:
            result = server.handler(op, args)
            self._reply({"ok": True, "result": result})
        except Exception as exc:  # noqa: BLE001 - report everything to the client
            log.warning("op %s failed: %s: %s", op, type(exc).__name__, exc)
            self._reply({"ok": False, "error": str(exc) or type(exc).__name__, "kind": type(exc).__name__})

    def _reply(self, obj: dict[str, Any]) -> None:
        try:
            self.wfile.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
            self.wfile.flush()
        except OSError:
            pass


class RpcServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    # SO_REUSEADDR is safe on POSIX (lets us rebind through TIME_WAIT) but on
    # Windows it would let another process hijack the port.
    allow_reuse_address = sys.platform != "win32"

    def __init__(self, port: int, token: str, handler: Handler) -> None:
        self.token = token
        self.handler = handler
        super().__init__(("127.0.0.1", int(port)), _RequestHandler)
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return int(self.server_address[1])

    def start(self) -> None:
        self._thread = threading.Thread(target=self.serve_forever, kwargs={"poll_interval": 0.5}, name="rpc", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.shutdown()
        self.server_close()


def bind_server(preferred_port: int, token: str, handler: Handler, fallback_ports: list[int] | None = None) -> RpcServer:
    """Bind to ``preferred_port``; if busy, try ``fallback_ports`` in order."""
    last: OSError | None = None
    for port in [preferred_port] + list(fallback_ports or []):
        try:
            return RpcServer(port, token, handler)
        except OSError as exc:
            last = exc
            continue
    raise RpcError(f"could not bind an RPC port: {last}")


__all__ = ["RpcError", "RpcRemoteError", "RpcServer", "bind_server", "call"]
