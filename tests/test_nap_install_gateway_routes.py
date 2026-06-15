#!/usr/bin/env python3
"""Smoke-test top-level nap gateway command routing."""

import importlib.util
import pathlib
import sys


def load_module():
    script_path = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "nap_install.py"
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
    assert calls[-1] == ("gateway", ["vault", "--help"])

    assert mod.handle_gateway(["vault", "list"]) == 0
    assert calls[-1] == ("gateway", ["vault", "list"])

    assert mod.handle_gateway(["vault", "rotate-secret", "--kind", "memory"]) == 0
    assert calls[-1] == ("gateway", ["vault", "rotate-secret", "--kind", "memory"])

    try:
        mod.handle_gateway(["process", "--all"])
    except RuntimeError as exc:
        assert "nap gateway vault process" in str(exc), str(exc)
    else:
        raise AssertionError("deprecated gateway process shortcut must not route")
    assert mod.main(["nap", "chat", "secret", "setup"]) == 0
    assert calls[-1] == ("chat", ["secret", "setup"])
    try:
        mod.handle_gateway(["config"])
    except RuntimeError as exc:
        assert "unknown gateway subcommand" in str(exc)
    else:
        raise AssertionError("gateway config alias must not route")
    try:
        mod.handle_gateway(["vault-setup", "list"])
    except RuntimeError as exc:
        assert "unknown gateway subcommand" in str(exc)
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
    # nap project bootstrap is now a deprecated alias handled by the MCP server
    # (the install side forwards it; the MCP side raises with a clear error message).
    forwarded = []
    mod.run_wrapper = lambda command, args: forwarded.append((command, args)) or 0
    mod.main(["nap", "project", "bootstrap"])
    assert forwarded == [("project", ["bootstrap"])], forwarded

    print("ok: nap installer gateway route smoke passed")


if __name__ == "__main__":
    run()
