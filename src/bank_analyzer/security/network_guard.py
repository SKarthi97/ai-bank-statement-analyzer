"""An application-level guard against outbound network connections.

``install_guard`` replaces the socket functions that open outbound connections or resolve names
so that anything other than loopback raises ``NetworkBlocked`` (an ``OSError``, which callers
already handle as a failed connection). Loopback stays open because the web UI and the local model
server use it.

This is defence in depth and NOT a sandbox. It only covers Python code in this process: Ollama,
Tesseract, the browser and any child process are outside it, code can undo it (it is plain Python),
and native extensions can open sockets without going through these functions. Real assurance needs an
operating-system firewall rule; see docs/04.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable, Iterable
from typing import Any

_LOOPBACK_NAMES = frozenset({"localhost", "localhost.localdomain"})


class NetworkBlocked(OSError):
    """An outbound connection to a non-loopback address was refused by the application guard."""


_installed: dict[str, Any] | None = None
_allowed: frozenset[str] = frozenset()


def is_loopback_host(host: object) -> bool:
    if isinstance(host, bytes):
        host = host.decode("ascii", "ignore")
    if not isinstance(host, str):
        return False
    host = host.strip("[]").split("%", 1)[0]
    if host.lower() in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _permitted(host: object) -> bool:
    if host in ("", None):  # wildcard / unspecified (bind-style lookups), not an outbound target
        return True
    if is_loopback_host(host):
        return True
    text = host.decode("ascii", "ignore") if isinstance(host, bytes) else str(host)
    return text.lower() in _allowed


def _check_address(address: object) -> None:
    if isinstance(address, tuple) and address:
        host = address[0]
        if not _permitted(host):
            raise NetworkBlocked("Outbound network connection blocked: only localhost is allowed.")
    # str/bytes addresses are AF_UNIX paths: local by definition


def install_guard(allow_hosts: Iterable[str] = ()) -> None:
    """Block non-loopback connections and name lookups in this process. Idempotent."""
    global _installed, _allowed
    _allowed = frozenset(h.lower() for h in allow_hosts)
    if _installed is not None:
        return
    original: dict[str, Any] = {
        "connect": socket.socket.connect,
        "connect_ex": socket.socket.connect_ex,
        "getaddrinfo": socket.getaddrinfo,
        "gethostbyname": socket.gethostbyname,
    }
    orig_connect: Callable[..., Any] = original["connect"]
    orig_connect_ex: Callable[..., Any] = original["connect_ex"]
    orig_getaddrinfo: Callable[..., Any] = original["getaddrinfo"]
    orig_gethostbyname: Callable[..., Any] = original["gethostbyname"]

    def connect(self: socket.socket, address: Any) -> Any:
        _check_address(address)
        return orig_connect(self, address)

    def connect_ex(self: socket.socket, address: Any) -> Any:
        _check_address(address)
        return orig_connect_ex(self, address)

    def getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        if not _permitted(host):
            raise NetworkBlocked("Name lookup blocked: only localhost is allowed.")
        return orig_getaddrinfo(host, *args, **kwargs)

    def gethostbyname(host: Any) -> Any:
        if not _permitted(host):
            raise NetworkBlocked("Name lookup blocked: only localhost is allowed.")
        return orig_gethostbyname(host)

    socket.socket.connect = connect  # type: ignore[method-assign,assignment]
    socket.socket.connect_ex = connect_ex  # type: ignore[method-assign,assignment]
    socket.getaddrinfo = getaddrinfo
    socket.gethostbyname = gethostbyname
    _installed = original


def uninstall_guard() -> None:
    """Restore the original socket functions."""
    global _installed
    if _installed is None:
        return
    socket.socket.connect = _installed["connect"]  # type: ignore[method-assign]
    socket.socket.connect_ex = _installed["connect_ex"]  # type: ignore[method-assign]
    socket.getaddrinfo = _installed["getaddrinfo"]
    socket.gethostbyname = _installed["gethostbyname"]
    _installed = None


def guard_active() -> bool:
    return _installed is not None
