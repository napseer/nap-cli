#!/usr/bin/env python3
"""Regression tests for bounded DNS address fallback in the updater transport."""

import importlib.util
import pathlib
import socket


def load_module():
    script_path = (
        pathlib.Path(__file__).resolve().parents[1]
        / "resources"
        / "scripts"
        / "nap_install.py"
    )
    spec = importlib.util.spec_from_file_location(
        "nap_install_address_fallback_test", script_path
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


def test_updater_prefers_ipv4_and_bounds_failed_addresses():
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


if __name__ == "__main__":
    test_updater_prefers_ipv4_and_bounds_failed_addresses()
    print("ok: updater connection address fallback tests passed")
