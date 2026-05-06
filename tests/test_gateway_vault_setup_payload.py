#!/usr/bin/env python3
"""Smoke-test client-wrapped project secret setup and rotation payload generation."""

import base64
import importlib.util
import pathlib
import sys


PROJECT_ID = "11111111-1111-1111-1111-111111111111"
ACCOUNT_ID = "22222222-2222-2222-2222-222222222222"
PASSPHRASE = "correct horse battery staple"


def load_module():
    script_path = pathlib.Path(__file__).resolve().parents[1] / "resources" / "scripts" / "napseer_mcp_server.py"
    sys.path.insert(0, str(script_path.parent))
    spec = importlib.util.spec_from_file_location("napseer_mcp_server", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def assert_wrapped_record(mod, record, secret_kind, data_key_epoch=1):
    assert record["schema"] == mod.WRAPPED_PROJECT_KEY_BUNDLE_SCHEMA
    assert record["project_id"] == PROJECT_ID
    assert record["account_id"] == ACCOUNT_ID
    assert record["kdf"] == "PBKDF2-HMAC-SHA256"
    assert record["kdf_params"]["iterations"] >= 600000
    assert record["expansion"] == "HKDF-SHA256"
    assert f":kind:{secret_kind}:" in record["hkdf_info"]
    assert record["aead_alg"] == "AES-256-GCM"
    assert record["aad_canonicalization"] == "json-sorted-keys-v1"
    assert record["status"] == "active"
    assert record["data_key_epoch"] == data_key_epoch
    assert len(base64.b64decode(record["salt_b64"], validate=True)) >= 16
    assert len(base64.b64decode(record["nonce_b64"], validate=True)) == 12
    bundle = mod.unwrap_project_key_bundle(record)
    assert set(bundle["keys"]) == {"chat", "tabs", "gateway", "memory"}
    for kind, state in bundle["keys"].items():
        version = str(state["active_version"])
        key = base64.b64decode(state["versions"][version]["key_b64"], validate=True)
        assert len(key) == 32, kind


def run():
    mod = load_module()
    writes = []

    mod.AUTH = {"agent_id": "gateway-agent-1", "account_id": ACCOUNT_ID}
    mod.GATEWAY_PROJECT_BUNDLE_PASSPHRASE = PASSPHRASE
    mod.VAULT_KEY = bytes(range(32))
    mod.VAULT_SECRETS = {}
    mod.persist_vault_secrets = lambda: writes.append(dict(mod.VAULT_SECRETS))

    payload = mod.gateway_generate_project_vault_setup_payload(PROJECT_ID, "setup-1")
    assert mod.VAULT_KDF_ITERATIONS == 600000
    assert mod.PROJECT_BUNDLE_KDF_ITERATIONS >= 600000
    assert not writes, "project secret payload generation must not persist local vault state"
    assert set(payload) == {"project_secrets"}
    assert "secret_b64" not in str(payload)
    assert "project_secret_b64" not in str(payload)
    assert set(item["secret_kind"] for item in payload["project_secrets"]) == {"chat", "tabs", "gateway", "memory"}

    for item in payload["project_secrets"]:
        assert set(item) == {"secret_kind", "wrapped_key_bundle"}
        assert_wrapped_record(mod, item["wrapped_key_bundle"], item["secret_kind"])

    assert "project_vaults" not in mod.VAULT_SECRETS
    assert "vault_master_secret_b64" not in str(mod.VAULT_SECRETS)
    assert "secret_b64" not in str(mod.VAULT_SECRETS)

    tools = {tool["name"]: tool for tool in mod.raw_tools()}
    assert "transient secret_b64" not in tools["nap_gateway_vault_setup_process"]["description"]
    assert "opaque client-wrapped key bundle records" in tools["nap_gateway_vault_setup_process"]["description"]
    assert "transient secret_b64" not in tools["nap_gateway_vault_secret_rotate"]["description"]
    assert "opaque client-wrapped key bundle record" in tools["nap_gateway_vault_secret_rotate"]["description"]

    filtered = mod.gateway_vault_secret_subset({
        "gateway_encryption_key": base64.b64encode(bytes(range(32))).decode("ascii"),
        "gateway_remote_enabled": True,
        "gateway_default_command": "bash",
        "project_vaults": {
            PROJECT_ID: {
                "vault_master_secret_b64": base64.b64encode(bytes(range(32))).decode("ascii"),
                "project_secrets": {
                    "chat": {"secret_b64": base64.b64encode(bytes(range(32))).decode("ascii")},
                },
            },
        },
    })
    assert filtered["gateway_default_command"] == "bash"
    assert filtered["gateway_remote_enabled"] is True
    assert "project_vaults" not in filtered
    assert "vault_master_secret_b64" not in str(filtered)
    assert "secret_b64" not in str(filtered)

    local_before = {
        "gateway_encryption_key": base64.b64encode(bytes(range(32))).decode("ascii"),
        "gateway_remote_enabled": True,
        "gateway_default_command": "bash",
    }
    mod.VAULT_SECRETS = dict(local_before)
    assert len(mod.gateway_generate_project_vault_setup_payload(PROJECT_ID, "setup-2")["project_secrets"]) == 4
    assert mod.VAULT_SECRETS == local_before

    vector_plaintext = mod.generate_project_data_key_bundle(PROJECT_ID, ACCOUNT_ID, data_key_epoch=7)
    vector_plaintext["bundle_id"] = "vector-bundle-001"
    for index, kind in enumerate(mod.GATEWAY_PROJECT_SECRET_KINDS):
        key_record = vector_plaintext["keys"][kind]["versions"]["7"]
        key_record["key_b64"] = base64.b64encode(bytes([index + 1]) * 32).decode("ascii")
        key_record["created_at"] = "2026-05-06T00:00:00Z"
    vector_record = mod.wrapped_project_key_bundle_record(
        PROJECT_ID,
        ACCOUNT_ID,
        "memory",
        vector_plaintext,
        wrapping_epoch=3,
        bundle_version=7,
        data_key_epoch=7,
        salt=bytes(range(16)),
        nonce=bytes(range(12)),
        created_at="2026-05-06T00:00:00Z",
    )
    assert vector_record["aad_hash"] == "sha256:22f60004f8e873b4e3d9bd3b15ab0ba4de358ba74fc799255d0488596f33fa68"
    assert vector_record["ciphertext_sha256"] == "sha256:5c59b781c3ad29843fb9f61efbd57ef6b19a963565a1e1f508bd18b20a6b6212"
    assert mod.unwrap_project_key_bundle(vector_record) == vector_plaintext

    calls = []
    active_bundle = mod.wrapped_project_key_bundle_record(
        PROJECT_ID,
        ACCOUNT_ID,
        "chat",
        mod.generate_project_data_key_bundle(PROJECT_ID, ACCOUNT_ID, data_key_epoch=1),
        wrapping_epoch=1,
        bundle_version=1,
        data_key_epoch=1,
    )

    def request_json(method, path, payload=None, **kwargs):
        assert (method, path) == ("GET", f"/v1/projects/{PROJECT_ID}/vault/secrets/chat/active")
        return {
            "secret_kind": "chat",
            "version": 1,
            "wrapping_epoch": 1,
            "bundle_version": 1,
            "data_key_epoch": 1,
            "wrapped_key_bundle": active_bundle,
        }

    mod.DEFAULT_PROJECT_ID = PROJECT_ID
    mod.require_unlocked = lambda operation="operation": None
    mod.request_json = request_json
    mod.request_project_write = lambda method, path, body, project_id, purpose, scope_type="project": calls.append(
        (method, path, body, project_id, purpose, scope_type)
    ) or {
        "id": "secret-version-2",
        "version": 2,
        "secret_kind": "chat",
    }
    rotated = mod.gateway_rotate_project_vault_secret({"project_id": PROJECT_ID, "secret_kind": "chat"})
    assert rotated["version"] == 2
    assert not writes, "rotation must not persist project secret material locally"
    assert calls[0][0] == "POST"
    assert calls[0][1] == f"/v1/projects/{PROJECT_ID}/vault/secrets/chat/rotate"
    assert calls[0][5] == "encryption"
    assert set(calls[0][2]) == {"wrapped_key_bundle"}
    assert_wrapped_record(mod, calls[0][2]["wrapped_key_bundle"], "chat", data_key_epoch=2)
    assert "secret_b64" not in str(calls[0][2])
    assert mod.VAULT_SECRETS == local_before

    print("ok: gateway vault setup payload smoke passed")


if __name__ == "__main__":
    run()
