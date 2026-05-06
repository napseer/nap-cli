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
    assert calls == []

    assert mod.handle_gateway(["vault", "list"]) == 0
    assert calls[-1] == ("gateway", ["vault", "list"])

    assert mod.handle_gateway(["vault", "rotate-secret", "--kind", "chat"]) == 0
    assert calls[-1] == ("gateway", ["vault", "rotate-secret", "--kind", "chat"])

    try:
        mod.handle_gateway(["vault-setup", "list"])
    except RuntimeError as exc:
        assert "unknown gateway command" in str(exc)
    else:
        raise AssertionError("deprecated gateway vault-setup alias must not route")

    assert mod.handle_gateway(["terminal", "list"]) == 0
    assert calls[-1] == ("gateway", ["terminal", "list"])

    print("ok: nap installer gateway route smoke passed")


if __name__ == "__main__":
    run()
