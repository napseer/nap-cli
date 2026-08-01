#!/usr/bin/env python3
"""Smoke-test top-level nap gateway command routing."""

import importlib.util
import pathlib
import sys


def load_module():
    script_path = pathlib.Path(__file__).resolve().parents[1] / "resources" / "scripts" / "nap_install.py"
    spec = importlib.util.spec_from_file_location("nap_install", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def run():
    mod = load_module()
    calls = []

    mod.run_wrapper = lambda command, args: calls.append((command, args)) or 0

    assert mod.handle_gateway(["--help"]) == 0
    assert calls == []

    assert mod.handle_gateway(["vault", "--help"]) == 0
    assert calls == []

    try:
        mod.handle_gateway(["vault", "list"])
    except RuntimeError as exc:
        assert "gateway vault status" in str(exc)
    else:
        raise AssertionError("removed gateway vault list alias must not route")

    assert mod.handle_gateway(["vault", "rotate-secret", "--kind", "memory"]) == 0
    assert calls[-1] == ("gateway", ["vault", "rotate-secret", "--kind", "memory"])

    try:
        mod.handle_gateway(["process", "--all"])
    except RuntimeError as exc:
        assert "nap gateway vault process" in str(exc), str(exc)
    else:
        raise AssertionError("deprecated gateway process shortcut must not route")
    try:
        mod.main(["nap", "chat", "secret", "setup"])
    except RuntimeError as exc:
        assert "MCP chat-secret tools" in str(exc)
    else:
        raise AssertionError("removed chat compatibility command must not route")
    try:
        mod.handle_gateway(["config"])
    except RuntimeError as exc:
        assert "unknown gateway command" in str(exc)
    else:
        raise AssertionError("gateway config alias must not route")
    try:
        mod.handle_gateway(["vault-setup", "list"])
    except RuntimeError as exc:
        assert "unknown gateway command" in str(exc)
    else:
        raise AssertionError("deprecated gateway vault-setup alias must not route")

    assert mod.handle_gateway(["terminal", "list"]) == 0
    assert calls[-1] == ("gateway", ["terminal", "list"])

    calls.clear()
    assert mod.main(["nap", "status"]) == 0
    assert calls[-1] == ("configure", [])

    # Install-level dispatch should reject: empty / config / configure / bootstrap / create / ui
    for argv in (
        ["nap", "config"],
        ["nap", "configure"],
        ["nap", "bootstrap"],
        ["nap", "create"],
        ["nap", "ui"],
    ):
        try:
            mod.main(argv)
        except RuntimeError as exc:
            assert "unknown" in str(exc), (argv, str(exc))
        else:
            raise AssertionError(f"deprecated route must not be accepted at install level: {argv}")
    try:
        mod.main(["nap", "project", "bootstrap"])
    except RuntimeError as exc:
        assert "unknown project command" in str(exc)
    else:
        raise AssertionError("deprecated project bootstrap alias must not route")

    print("ok: nap installer gateway route smoke passed")


if __name__ == "__main__":
    run()
