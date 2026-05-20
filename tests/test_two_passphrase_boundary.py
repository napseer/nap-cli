#!/usr/bin/env python3
"""Smoke-test vault/content and gateway relay passphrases stay separate."""

import importlib.util
import os
import pathlib
import sys


PROJECT_ID = "11111111-1111-1111-1111-111111111111"
ACCOUNT_ID = "22222222-2222-2222-2222-222222222222"


def load_module():
    script_path = pathlib.Path(__file__).resolve().parents[1] / "resources" / "scripts" / "napseer_mcp_server.py"
    sys.path.insert(0, str(script_path.parent))
    spec = importlib.util.spec_from_file_location("napseer_mcp_server_two_passphrase_test", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def run():
    mod = load_module()
    original_env = dict(os.environ)
    try:
        os.environ.pop("NAPSEER_VAULT_PASSPHRASE", None)
        os.environ.pop("NAPSEER_MASTER_PASSPHRASE", None)
        os.environ["NAPSEER_GATEWAY_PASSPHRASE"] = "gateway-only"
        mod.PROJECT_VAULT_PASSPHRASE = None

        try:
            mod.gateway_generate_project_vault_setup_payload(PROJECT_ID, "setup-1")
        except RuntimeError as exc:
            assert "NAPSEER_VAULT_PASSPHRASE" in str(exc)
        else:
            raise AssertionError("gateway relay passphrase must not wrap project bundles")

        os.environ["NAPSEER_VAULT_PASSPHRASE"] = "vault-content"
        mod.AUTH = {"agent_id": "gateway-agent-1", "account_id": ACCOUNT_ID}
        payload = mod.gateway_generate_project_vault_setup_payload(PROJECT_ID, "setup-2")
        assert {item["secret_kind"] for item in payload["project_secrets"]} == {"chat", "tabs", "gateway", "memory"}
        first = payload["project_secrets"][0]["wrapped_key_bundle"]
        assert mod.unwrap_project_key_bundle(first)["project_id"] == PROJECT_ID

        os.environ["NAPSEER_VAULT_PASSPHRASE"] = "wrong-vault-content"
        mod.PROJECT_VAULT_PASSPHRASE = None
        try:
            mod.unwrap_project_key_bundle(first)
        except RuntimeError:
            pass
        else:
            raise AssertionError("project bundle unwrap must require the vault/content passphrase")

        mod.PROJECT_VAULT_PASSPHRASE = "manual-vault"
        assert mod.project_bundle_passphrase() == "manual-vault"
    finally:
        os.environ.clear()
        os.environ.update(original_env)

    print("ok: two passphrase boundary smoke passed")


if __name__ == "__main__":
    run()
