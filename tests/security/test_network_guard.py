"""The application-level guard blocks non-loopback connections and lookups, and can be removed."""

from __future__ import annotations

import socket
import threading
from collections.abc import Iterator

import pytest

from bank_analyzer.security.network_guard import (
    NetworkBlocked,
    guard_active,
    install_guard,
    is_loopback_host,
    uninstall_guard,
)


@pytest.fixture
def guard() -> Iterator[None]:
    install_guard()
    yield
    uninstall_guard()


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("127.0.0.1", True),
        ("127.5.5.5", True),
        ("::1", True),
        ("[::1]", True),
        ("localhost", True),
        ("LOCALHOST", True),
        (b"127.0.0.1", True),
        ("8.8.8.8", False),
        ("192.168.1.10", False),
        ("example.com", False),
        ("127.0.0.1.evil.example", False),
        ("", False),
        (None, False),
        (5, False),
    ],
)
def test_is_loopback_host(host: object, expected: bool) -> None:
    assert is_loopback_host(host) is expected


class TestGuard:
    def test_blocks_connection_to_a_public_address(self, guard: None) -> None:
        with pytest.raises(NetworkBlocked):
            socket.create_connection(("93.184.216.34", 80), timeout=1)

    def test_blocks_a_direct_connect_and_connect_ex(self, guard: None) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            with pytest.raises(NetworkBlocked):
                sock.connect(("10.0.0.5", 9))
            with pytest.raises(NetworkBlocked):
                sock.connect_ex(("10.0.0.5", 9))
        finally:
            sock.close()

    def test_blocks_name_lookups(self, guard: None) -> None:
        with pytest.raises(NetworkBlocked):
            socket.getaddrinfo("example.com", 443)
        with pytest.raises(NetworkBlocked):
            socket.gethostbyname("example.com")

    def test_the_block_is_an_oserror_so_callers_treat_it_as_unreachable(self, guard: None) -> None:
        with pytest.raises(OSError):
            socket.create_connection(("93.184.216.34", 80), timeout=1)

    def test_loopback_still_works(self, guard: None) -> None:
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        accepted: list[bytes] = []
        thread = threading.Thread(target=lambda: accepted.append(server.accept()[0].recv(4)))
        thread.start()
        with socket.create_connection(("127.0.0.1", port), timeout=2) as client:
            client.sendall(b"ping")
        thread.join(2)
        server.close()
        assert accepted == [b"ping"]
        socket.getaddrinfo("localhost", port)  # lookups of localhost are allowed

    def test_explicitly_allowed_host_passes_the_guard(self) -> None:
        install_guard(allow_hosts=["model.internal.example"])
        try:
            # the guard lets it through; there is no such host, so the real lookup fails normally
            with pytest.raises(OSError) as info:
                socket.getaddrinfo("model.internal.example", 11434)
            assert not isinstance(info.value, NetworkBlocked)
        finally:
            uninstall_guard()

    def test_install_is_idempotent_and_uninstall_restores(self) -> None:
        original = socket.socket.connect
        install_guard()
        install_guard()
        assert guard_active()
        assert socket.socket.connect is not original
        uninstall_guard()
        assert not guard_active()
        assert socket.socket.connect is original
        uninstall_guard()  # harmless when not installed
