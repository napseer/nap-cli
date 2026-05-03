#!/usr/bin/env python3
"""Smoke-test parity between CLI and MCP gateway configure bootstrap flows."""

import importlib.util
import os
import pathlib
import sys


def load_module():
    script_path = pathlib.Path(__file__).resolve().parents[1] / "resources" / "scripts" / "napseer_mcp_server.py"
    sys.path.insert(0, str(script_path.parent))
    spec = importlib.util.spec_from_file_location("napseer_mcp_server", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def assert_equal(actual, expected, message):
    if actual != expected:
        raise AssertionError(f"{message}: expected={expected!r} actual={actual!r}")


def run():
    mod = load_module()
    calls = {"preregister": [], "setup": [], "unlock": [], "tmux": []}

    mod.AUTH = {}
    mod.TOKEN = None
    mod.load_public_auth_file = lambda: {}
    mod.vault_exists = lambda: False
    mod.gateway_is_unlocked = lambda: False
    mod.gateway_setup = lambda payload: calls["setup"].append(payload) or {"status": "configured"}
    mod.gateway_unlock = lambda passphrase: calls["unlock"].append(passphrase) or {"status": "unlocked"}
    mod.gateway_tmux_configure = lambda payload: calls["tmux"].append(payload) or {"status": "configured"}
    mod.gateway_status = lambda: {"status": "ok"}
    mod.gateway_log = lambda *args, **kwargs: None
    mod.ensure_container_uuid = lambda: "2a164e32-9d98-4b4e-9ad8-eed7307a5b08"
    mod.normalize_gateway_command = lambda: "bash"
    mod.gateway_bootstrap_token_invalid_result = lambda: {"status": "bootstrap_token_invalid"}

    def preregister(payload):
        calls["preregister"].append(payload)
        return {"status": "pending_review", "registration": {"id": "reg_1"}}

    mod.gateway_service_preregister = preregister

    os.environ["NAPSEER_GATEWAY_BOOTSTRAP_TOKEN"] = "npb_env"
    os.environ["NAPSEER_GATEWAY_PASSPHRASE"] = "pw_env"
    mcp_result = mod.mcp_gateway_configure({"default_command": "bash -l", "display_name": "gw-mcp"})
    assert_equal(mcp_result["status"], "pending_review", "mcp preregister status")

    cli_result = mod.cli_gateway_configure(["--command", "bash -l", "--name", "gw-cli"])
    assert_equal(cli_result["status"], "pending_review", "cli preregister status")

    positional_result = mod.cli_gateway_configure(["npb_positional", "--passphrase", "pw_positional", "--command", "bash"])
    assert_equal(positional_result["status"], "pending_review", "cli positional preregister status")

    assert_equal(len(calls["preregister"]), 3, "preregister call count")
    assert_equal(calls["preregister"][0]["bootstrap_token"], "npb_env", "mcp bootstrap token source")
    assert_equal(calls["preregister"][1]["bootstrap_token"], "npb_env", "cli bootstrap token source")
    assert_equal(calls["preregister"][2]["bootstrap_token"], "npb_positional", "cli positional bootstrap token source")
    assert_equal(calls["preregister"][0]["passphrase"], "pw_env", "mcp passphrase source")
    assert_equal(calls["preregister"][1]["passphrase"], "pw_env", "cli passphrase source")
    assert_equal(calls["preregister"][2]["passphrase"], "pw_positional", "cli positional passphrase source")
    assert_equal(calls["preregister"][0]["default_command"], "bash -l", "mcp default command")
    assert_equal(calls["preregister"][1]["default_command"], "bash -l", "cli default command")

    mod.AUTH = {"service_registration_id": "stale_reg"}
    mod.load_public_auth_file = lambda: {"service_registration_id": "stale_reg"}
    stale_result = mod.mcp_gateway_configure({"bootstrap_token": "npb_fresh", "passphrase": "pw", "default_command": "bash"})
    assert_equal(stale_result["status"], "pending_review", "stale registration should not block fresh bootstrap")
    assert_equal(calls["preregister"][-1]["bootstrap_token"], "npb_fresh", "fresh bootstrap token source")

    def preregister_invalid(_payload):
        raise RuntimeError("service bootstrap token is invalid or expired")

    mod.gateway_service_preregister = preregister_invalid
    invalid_mcp = mod.mcp_gateway_configure({"bootstrap_token": "npb_invalid", "passphrase": "pw", "default_command": "bash"})
    invalid_cli = mod.cli_gateway_configure(["--bootstrap-token", "npb_invalid", "--passphrase", "pw", "--command", "bash"])
    assert_equal(invalid_mcp["status"], "bootstrap_token_invalid", "mcp invalid token fallback")
    assert_equal(invalid_cli["status"], "bootstrap_token_invalid", "cli invalid token fallback")

    calls["preregister"].clear()
    calls["unlock"].clear()
    calls["tmux"].clear()
    mod.gateway_service_preregister = preregister
    mod.TOKEN = None
    mod.vault_exists = lambda: True
    mod.gateway_is_unlocked = lambda: False

    def unlock_existing(passphrase):
        calls["unlock"].append(passphrase)
        mod.TOKEN = "np_existing_worker"
        return {"status": "unlocked"}

    mod.gateway_unlock = unlock_existing
    preserved_mcp = mod.mcp_gateway_configure({
        "bootstrap_token": "npb_existing",
        "passphrase": "pw_existing",
        "default_command": "bash",
    })
    preserved_cli = mod.cli_gateway_configure([
        "--bootstrap-token", "npb_existing",
        "--passphrase", "pw_existing",
        "--command", "bash",
    ])
    assert_equal(preserved_mcp["status"], "configured", "mcp preserved gateway should skip preregister")
    assert_equal(preserved_cli["status"], "configured", "cli preserved gateway should skip preregister")
    assert_equal(calls["preregister"], [], "preserved gateway preregister calls")
    assert_equal(calls["unlock"], ["pw_existing", "pw_existing"], "preserved gateway unlocks before preregister")
    assert_equal(len(calls["tmux"]), 2, "preserved gateway command updates")

    calls["preregister"].clear()
    calls["unlock"].clear()
    mod.TOKEN = None
    mod.vault_exists = lambda: True
    service_unlocked = {"value": False}
    mod.gateway_is_unlocked = lambda: service_unlocked["value"]
    os.environ["NAPSEER_GATEWAY_PASSPHRASE"] = "pw_service"
    os.environ["NAPSEER_GATEWAY_BOOTSTRAP_TOKEN"] = "npb_service"
    mod.start_local_ui = lambda payload: {"status": "started", "payload": payload}

    def service_unlock(passphrase):
        calls["unlock"].append(passphrase)
        mod.TOKEN = "np_existing_worker"
        service_unlocked["value"] = True
        return {"status": "unlocked"}

    mod.gateway_unlock = service_unlock
    mod.threading.Event = lambda: type("StopEvent", (), {"wait": lambda self: (_ for _ in ()).throw(KeyboardInterrupt())})()
    service_result = mod.gateway_service_run({"timeout_seconds": 0})
    assert_equal(service_result["status"], "started", "service run with preserved token")
    assert_equal(calls["preregister"], [], "service run preserved gateway preregister calls")
    assert_equal(calls["unlock"], ["pw_service"], "service run unlocks before preregister")

    print("ok: gateway configure CLI/MCP parity smoke passed")


if __name__ == "__main__":
    run()
