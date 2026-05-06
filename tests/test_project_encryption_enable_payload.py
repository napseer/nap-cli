#!/usr/bin/env python3
"""Smoke-test project encryption enable fail-closed behavior."""

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


def assert_raises_hashicorp_closed(fn):
    try:
        fn()
    except RuntimeError as exc:
        message = str(exc)
        assert "HashiCorp" in message
        assert "disabled" in message
        assert "setup-request" in message
    else:
        raise AssertionError("CLI direct project encryption enable must fail closed")


def run():
    mod = load_module()
    calls = []

    mod.DEFAULT_PROJECT_ID = "project-1"
    mod.AUTH = {"agent_id": "agent-1"}
    mod.request_project_write = lambda method, path, body, project_id, purpose, scope_type="project": calls.append(
        (method, path, body, project_id, purpose, scope_type)
    ) or {"state": "plaintext"}
    mod.create_project_with_state = lambda args: ({"id": "project-1"}, "created", "created")
    mod.save_auth = lambda payload: None

    assert_raises_hashicorp_closed(lambda: mod.project_encryption_transition({"state": "encrypted", "passphrase": "pw"}))
    assert calls == []

    assert_raises_hashicorp_closed(lambda: mod.bootstrap_project({"encryption": "encrypted", "passphrase": "pw"}))
    assert calls == []

    result = mod.project_encryption_transition({"state": "plaintext"})
    assert result["status"] == "updated"
    assert calls, "plaintext transition should still call backend"
    method, path, body, project_id, purpose, scope_type = calls[0]
    assert method == "POST"
    assert path == "/v1/projects/project-1/encryption/disable"
    assert project_id == "project-1"
    assert scope_type == "encryption"
    assert body == {"state": "plaintext"}
    assert "wrapped" not in str(body)

    print("ok: project encryption enable fail-closed smoke passed")


if __name__ == "__main__":
    run()
