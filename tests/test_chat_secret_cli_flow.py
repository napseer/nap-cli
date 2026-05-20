#!/usr/bin/env python3
"""Smoke-test simplified chat secret CLI flow."""

import importlib.util
import pathlib
import sys


PROJECT_ID = "11111111-1111-1111-1111-111111111111"


def load_module():
    script_path = pathlib.Path(__file__).resolve().parents[1] / "resources" / "scripts" / "napseer_mcp_server.py"
    sys.path.insert(0, str(script_path.parent))
    spec = importlib.util.spec_from_file_location("napseer_mcp_server_chat_secret_test", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def run():
    mod = load_module()
    processed = []

    mod.current_project_id = lambda: PROJECT_ID
    mod.gateway_is_unlocked = lambda: True
    mod.vault_exists = lambda: True
    mod.gateway_vault_setup_requests = lambda args=None: {
        "items": [{"id": "setup-1", "status": "pending", "project_id": PROJECT_ID}]
    }
    mod.gateway_process_vault_setup_requests = lambda args=None: processed.append(args) or {
        "status": "processed",
        "completed_count": 1,
    }

    def request_json(method, path, payload=None, **kwargs):
        assert method == "GET"
        assert path == f"/v1/projects/{PROJECT_ID}/vault/secrets?secret_kind=chat"
        return {"items": []}

    mod.request_json = request_json
    setup = mod.chat_secret_setup({"project_id": PROJECT_ID, "vault_passphrase": "vault-content"})
    assert mod.PROJECT_VAULT_PASSPHRASE == "vault-content"
    assert processed and processed[-1]["complete_all"] is True
    assert setup["chat_secret_configured"] is False

    mod.gateway_vault_setup_requests = lambda args=None: {"items": []}
    mod.request_json = lambda method, path, payload=None, **kwargs: {
        "items": [{
            "secret_kind": "chat",
            "version": 3,
            "status": "active",
            "activation_status": "active",
            "storage_provider": "hashicorp_vault_kv2",
            "last_rotated_at": "2026-05-19T20:00:00Z",
        }]
    }
    status = mod.chat_secret_status({"project_id": PROJECT_ID})
    assert status["passphrase_authority"] == "vault"
    assert status["chat_secret_configured"] is True
    assert status["active_chat_secret"]["version"] == 3

    rotation = mod.chat_secret_rotate({"project_id": PROJECT_ID})
    assert rotation["status"] == "operator_auth_required"
    assert "worker token" in rotation["message"]

    print("ok: chat secret CLI flow smoke passed")


if __name__ == "__main__":
    run()
