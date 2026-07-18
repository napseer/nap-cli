#!/usr/bin/env python3
"""Smoke-test auth refresh with local gateway storage available."""

import importlib.util
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


def run():
    mod = load_module()
    writes = []

    vault_writes = []
    mod.VAULT_SECRETS = {}
    mod.VAULT_KEY = b"k" * 32
    mod.AUTH = {
        "base_url": "https://api.example.test",
        "project_id": "project_1",
        "worker_name": "agent",
        "device_fingerprint": "device",
        "root_path": "/repo",
        "worker_capabilities": {"local_mcp": True},
    }
    mod.BASE_URL = mod.AUTH["base_url"]
    mod.DEFAULT_PROJECT_ID = mod.AUTH["project_id"]
    mod.TOKEN = "expired"
    mod.TOKEN_EXPIRES_AT = "old"
    mod.vault_exists = lambda: True
    mod.gateway_is_unlocked = lambda: True
    mod.load_public_auth_file = lambda: {
        "base_url": "https://api.example.test",
        "project_id": "project_1",
        "worker_name": "agent",
        "device_fingerprint": "device",
        "root_path": "/repo",
        "worker_capabilities": {"local_mcp": True},
    }
    mod.write_public_auth = lambda payload: writes.append(dict(payload))

    mod.require_unlocked = lambda operation="operation": None
    mod.write_vault_with_key = lambda key, payload: vault_writes.append((key, payload))

    mod.save_auth({"token": "fresh", "token_expires_at": "new", "worker_id": "worker_1"})
    assert writes[-1]["token"] == "fresh"
    assert writes[-1]["token_expires_at"] == "new"
    assert mod.TOKEN == "fresh"
    assert mod.TOKEN_EXPIRES_AT == "new"

    mod.load_public_auth_file = lambda: {
        "base_url": "https://api.example.test",
        "project_id": "project_1",
        "worker_name": "agent",
        "device_fingerprint": "device",
        "root_path": "/repo",
        "worker_capabilities": {"local_mcp": True, "gateway": True},
        "token": "newer",
        "token_expires_at": "later",
    }
    mod.refresh_public_auth_state()
    assert mod.TOKEN == "newer"
    assert mod.TOKEN_EXPIRES_AT == "later"

    mod.save_auth({"gateway_default_command": "bash -l"})
    assert vault_writes[-1][0] == b"k" * 32
    assert vault_writes[-1][1]["secrets"]["gateway_default_command"] == "bash -l"

    oauth_saves = []
    mod.AUTH.update({
        "account_mode": "operator_project",
        "oauth_client_id": "nap-cli",
        "refresh_token": "old-oauth-refresh",
    })
    mod.REFRESH_TOKEN = "old-oauth-refresh"
    mod.REFRESH_EXPIRES_AT = "old-refresh-expiry"
    mod.request_form_json = lambda method, path, payload, token_required=False: {
        "access_token": "fresh-oauth-access",
        "refresh_token": "fresh-oauth-refresh",
        "refresh_expires_at": "new-refresh-expiry",
        "expires_in": 3600,
        "project_id": "project_1",
    }
    mod.save_auth = lambda updates: oauth_saves.append(dict(updates))
    mod.request_json = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("OAuth refresh must not call the enrollment endpoint")
    )
    result = mod.renew_auth()
    assert result["method"] == "oauth_refresh"
    assert oauth_saves[0]["refresh_token"] == "fresh-oauth-refresh"
    assert oauth_saves[0]["refresh_expires_at"] == "new-refresh-expiry"

    print("ok: local gateway auth refresh smoke passed")


if __name__ == "__main__":
    run()
