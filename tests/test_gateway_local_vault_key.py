#!/usr/bin/env python3
"""Smoke-test local gateway storage uses a machine-local key, not interactive unlock."""

import importlib.util
import pathlib
import sys
import tempfile


def load_module():
    script_path = pathlib.Path(__file__).resolve().parents[1] / "resources" / "scripts" / "napseer_mcp_server.py"
    sys.path.insert(0, str(script_path.parent))
    spec = importlib.util.spec_from_file_location("napseer_mcp_server_local_vault_test", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def run():
    mod = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        state = pathlib.Path(tmp)
        mod.AUTH_DIR = state
        mod.AUTH_PATH = state / "auth.json"
        mod.VAULT_PATH = state / "vault.json"
        mod.LOCAL_VAULT_KEY_PATH = state / "vault.local-key"
        mod.GATEWAY_RELAY_SECRET_PATH = state / "gateway-relay-secret.json"
        mod.GATEWAY_RELAY_STATE_PATH = state / "gateway-relay-state.json"
        mod.CONTAINER_IDENTITY_PATH = state / "container-identity.json"
        mod.TELEMETRY_PATH = state / "telemetry.json"

        auth = {
            "base_url": "https://api.example.test",
            "project_id": "project-1",
            "token": "token",
            "token_expires_at": "later",
            "ssh_key_path": str(state / "id_ed25519"),
        }
        mod.AUTH = dict(auth)
        mod.BASE_URL = auth["base_url"]
        mod.DEFAULT_PROJECT_ID = auth["project_id"]
        mod.TOKEN = auth["token"]
        mod.TOKEN_EXPIRES_AT = auth["token_expires_at"]
        mod.load_auth = lambda public_override=None, secret_override=None: {
            **auth,
            **(public_override or {}),
            **(secret_override or {}),
        }
        mod.ensure_gateway_worker_capability = lambda refresh=True: {"status": "updated"}

        result = mod.gateway_setup({"passphrase": "relay-passphrase", "default_command": "bash -l"})
        assert result["status"] == "local_available"
        assert mod.LOCAL_VAULT_KEY_PATH.exists()
        assert mod.GATEWAY_RELAY_SECRET_PATH.exists()
        assert mod.read_vault(mod.LOCAL_VAULT_KEY_PATH.read_text(encoding="utf-8").strip())["secrets"]["gateway_default_command"] == "bash -l"

        mod.VAULT_SECRETS = {}
        mod.VAULT_KEY = None
        mod.GATEWAY_UNLOCKED = False
        opened = mod.gateway_open_local()
        assert opened["status"] == "local_available"
        assert mod.VAULT_SECRETS["gateway_default_command"] == "bash -l"

    print("ok: local gateway vault key smoke passed")


if __name__ == "__main__":
    run()
