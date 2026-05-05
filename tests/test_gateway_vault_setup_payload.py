#!/usr/bin/env python3
"""Smoke-test gateway-owned project vault setup payload generation."""

import base64
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

    mod.AUTH = {"agent_id": "gateway-agent-1"}
    mod.VAULT_KEY = bytes(range(32))
    mod.VAULT_SECRETS = {}
    mod.persist_vault_secrets = lambda: writes.append(dict(mod.VAULT_SECRETS))

    payload = mod.gateway_generate_project_vault_setup_payload("project-1", "setup-1")
    assert mod.VAULT_KDF_ITERATIONS == 600000
    assert writes, "payload generation should persist local encrypted vault state"
    assert set(item["secret_kind"] for item in payload["project_secrets"]) == {"chat", "tabs", "gateway"}
    assert payload["wrapped_master_secret"]["credential_id"] == "gateway-vault:gateway-agent-1"
    assert payload["wrapped_master_secret"]["kdf_params"]["iterations"] == 600000

    for item in payload["project_secrets"]:
        envelope = item["encrypted_secret_envelope"]
        assert envelope["schema_version"] == 1
        assert envelope["alg"] == "AES-GCM-256"
        assert envelope["key_id"] == "vault-master:project-1:v1"
        assert "plaintext" not in envelope
        assert "content" not in envelope
        assert len(base64.b64decode(envelope["nonce_b64"])) == 12
        assert len(base64.b64decode(envelope["ciphertext_b64"])) == envelope["payload_size_bytes"]
        assert envelope["payload_size_bytes"] > 16

    stored = mod.VAULT_SECRETS["project_vaults"]["project-1"]
    assert stored["setup_request_id"] == "setup-1"
    assert len(base64.b64decode(stored["vault_master_secret_b64"])) == 32
    assert set(stored["project_secrets"]) == {"chat", "tabs", "gateway"}

    writes.clear()
    calls = []
    mod.DEFAULT_PROJECT_ID = "project-1"
    mod.require_unlocked = lambda operation="operation": None
    mod.request_project_write = lambda method, path, body, project_id, purpose, scope_type="project": calls.append(
        (method, path, body, project_id, purpose, scope_type)
    ) or {
        "id": "secret-version-2",
        "version": 2,
        "secret_kind": "chat",
    }
    rotated = mod.gateway_rotate_project_vault_secret({"project_id": "project-1", "secret_kind": "chat"})
    assert rotated["version"] == 2
    assert calls[0][0] == "POST"
    assert calls[0][1] == "/v1/projects/project-1/vault/secrets/chat/rotate"
    assert calls[0][5] == "encryption"
    assert calls[0][2]["encrypted_secret_envelope"]["key_id"] == "vault-master:project-1:v1"
    assert mod.VAULT_SECRETS["project_vaults"]["project-1"]["project_secrets"]["chat"]["version"] == 2

    print("ok: gateway vault setup payload smoke passed")


if __name__ == "__main__":
    run()
