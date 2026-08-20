#!/usr/bin/env python3
"""Regression tests for bounded DNS address fallback in the local wrapper."""

import importlib.util
import pathlib
import socket
import sys


def load_module():
    script_path = (
        pathlib.Path(__file__).resolve().parents[1]
        / "resources"
        / "scripts"
        / "napseer_mcp_server.py"
    )
    sys.path.insert(0, str(script_path.parent))
    spec = importlib.util.spec_from_file_location(
        "napseer_mcp_server_address_fallback_test", script_path
    )
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class FakeSocket:
    def __init__(self, family, attempts, failing_addresses):
        self.family = family
        self.attempts = attempts
        self.failing_addresses = failing_addresses
        self.timeouts = []
        self.closed = False

    def settimeout(self, value):
        self.timeouts.append(value)

    def bind(self, _address):
        return None

    def connect(self, address):
        self.attempts.append((self.family, address[0]))
        if address[0] in self.failing_addresses:
            raise TimeoutError("unreachable test address")

    def close(self):
        self.closed = True


def test_ipv4_is_preferred_and_failed_addresses_are_bounded():
    mod = load_module()
    attempts = []
    sockets = []

    def resolver(_host, port, _family, _socktype):
        return [
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2001:db8::1", port, 0, 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", port)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.2", port)),
        ]

    def factory(family, _socktype, _proto):
        created = FakeSocket(family, attempts, {"192.0.2.1"})
        sockets.append(created)
        return created

    connected = mod.connect_with_address_fallback(
        ("api.example.test", 443),
        timeout=30,
        resolver=resolver,
        socket_factory=factory,
    )

    assert connected is sockets[1]
    assert attempts == [
        (socket.AF_INET, "192.0.2.1"),
        (socket.AF_INET, "192.0.2.2"),
    ]
    assert sockets[0].timeouts == [mod.API_CONNECT_ATTEMPT_TIMEOUT_SECONDS]
    assert sockets[0].closed
    assert sockets[1].timeouts == [mod.API_CONNECT_ATTEMPT_TIMEOUT_SECONDS, 30.0]


def test_ipv4_only_never_attempts_ipv6():
    mod = load_module()
    attempts = []
    resolver_families = []

    def resolver(_host, port, family, _socktype):
        resolver_families.append(family)
        return [
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2001:db8::1", port, 0, 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", port)),
        ]

    def factory(family, _socktype, _proto):
        return FakeSocket(family, attempts, {"192.0.2.1"})

    try:
        mod.connect_with_address_fallback(
            ("api.example.test", 443),
            timeout=30,
            resolver=resolver,
            socket_factory=factory,
            ipv4_only=True,
        )
    except TimeoutError:
        pass
    else:
        raise AssertionError("IPv4-only connection unexpectedly succeeded")

    assert resolver_families == [socket.AF_INET]
    assert attempts == [(socket.AF_INET, "192.0.2.1")]


if __name__ == "__main__":
    test_ipv4_is_preferred_and_failed_addresses_are_bounded()
    test_ipv4_only_never_attempts_ipv6()
    print("ok: connection address fallback tests passed")
