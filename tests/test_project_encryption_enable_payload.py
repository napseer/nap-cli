#!/usr/bin/env python3
"""Smoke-test project encryption enable payload generation."""

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
    calls = []

    mod.DEFAULT_PROJECT_ID = "project-1"
    mod.AUTH = {"agent_id": "agent-1"}
    mod.request_project_write = lambda method, path, body, project_id, purpose, scope_type="project": calls.append(
        (method, path, body, project_id, purpose, scope_type)
    ) or {"state": "encrypted", "active_project_key_version": {"version": 1}}
    mod.save_auth = lambda payload: None

    result = mod.project_encryption_transition({"state": "encrypted", "passphrase": "pw"})
    assert result["status"] == "updated"
    assert calls, "encryption enable should call backend"
    method, path, body, project_id, purpose, scope_type = calls[0]
    assert method == "POST"
    assert path == "/v1/projects/project-1/encryption/enable"
    assert project_id == "project-1"
    assert scope_type == "encryption"
    assert body["state"] == "encrypted"
    wrapped = body["wrapped_project_key"]
    assert wrapped["credential_id"] == "local-passphrase:agent-1"
    assert wrapped["wrap_alg"] == "AES-GCM-256"
    assert wrapped["kdf_alg"] == "PBKDF2-SHA256"
    assert wrapped["kdf_params"]["source"] == "local_passphrase"
    assert wrapped["kdf_params"]["iterations"] == mod.PROJECT_KEY_KDF_ITERATIONS
    assert len(base64.b64decode(wrapped["salt_b64"])) == 16
    assert len(base64.b64decode(wrapped["kdf_params"]["nonce_b64"])) == 12
    assert len(base64.b64decode(wrapped["wrapped_project_key_b64"])) == 48

    calls.clear()
    result = mod.project_encryption_transition({"state": "plaintext"})
    assert result["status"] == "updated"
    assert "wrapped_project_key" not in calls[0][2]

    print("ok: project encryption enable payload smoke passed")


if __name__ == "__main__":
    run()
