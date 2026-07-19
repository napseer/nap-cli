#!/usr/bin/env python3
"""Focused tests for anonymous credential persistence and recovery."""

import importlib.util
import pathlib
import sys
import tempfile


def load_module():
    script_path = pathlib.Path(__file__).resolve().parents[1] / "resources" / "scripts" / "napseer_mcp_server.py"
    sys.path.insert(0, str(script_path.parent))
    spec = importlib.util.spec_from_file_location("napseer_mcp_server_anonymous_recovery_test", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def anonymous_auth(mod):
    return {
        "base_url": "https://api.example.test",
        "account_mode": "anonymous",
        "worker_name": "agent",
        "device_fingerprint": "device",
        "root_path": "/repo",
        "worker_capabilities": {"local_mcp": True},
        "ssh_key_path": "/tmp/test-key",
    }


def test_initial_enrollment_persists_refresh_credential():
    mod = load_module()
    mod.TOKEN = None
    mod.AUTH = anonymous_auth(mod)
    mod.BASE_URL = mod.AUTH["base_url"]
    mod.DEFAULT_PROJECT_ID = None
    with tempfile.TemporaryDirectory() as directory:
        key_path = pathlib.Path(directory) / "test-key"
        pathlib.Path(str(key_path) + ".pub").write_text(
            "ssh-ed25519 test-public-key", encoding="utf-8"
        )
        mod.ensure_local_files = lambda: key_path
        mod.sign_text = lambda *args: "test-signature"
        saved = []
        mod.save_auth = lambda payload: saved.append(dict(payload))
        responses = iter([
            {"challenge_id": "challenge", "challenge_text": "sign-me"},
            {
                "token": {
                    "access_token": "access",
                    "refresh_token": "refresh",
                    "expires_at": "2099-01-01T00:00:00Z",
                    "refresh_expires_at": "",
                },
                "worker": {"id": "worker", "agent_id": "agent-id"},
            },
        ])
        mod.request_json = lambda *args, **kwargs: next(responses)

        mod.ensure_enrolled({"slug": "example"})

    assert saved[0]["refresh_token"] == "refresh"
    assert "refresh_expires_at" in saved[0]


def test_failed_anonymous_refresh_uses_ssh_recovery():
    mod = load_module()
    mod.AUTH = anonymous_auth(mod)
    mod.BASE_URL = mod.AUTH["base_url"]
    mod.DEFAULT_PROJECT_ID = "project"
    mod.REFRESH_TOKEN = "stale-refresh"
    mod.REFRESH_EXPIRES_AT = None
    mod.auth_public_key = lambda: "ssh-ed25519 test-public-key"
    mod.sign_with_auth_key = lambda *args: "test-signature"
    saved = []
    mod.save_auth = lambda payload: saved.append(dict(payload))

    def request(method, path, payload=None, token_required=False):
        if path == "/v1/enrollment/refresh":
            raise RuntimeError("HTTP 401")
        if path == "/v1/enrollment/challenges":
            return {"challenge_id": "challenge", "challenge_text": "sign-me"}
        if path == "/v1/enrollment/verify":
            return {
                "token": {
                    "access_token": "recovered-access",
                    "refresh_token": "recovered-refresh",
                    "expires_at": "2099-01-01T00:00:00Z",
                    "refresh_expires_at": "",
                },
                "worker": {
                    "id": "worker",
                    "agent_id": "agent-id",
                    "name": "agent",
                    "device_fingerprint": "device",
                    "root_path": "/repo",
                    "project_id": "project",
                },
            }
        raise AssertionError(path)

    mod.request_json = request
    result = mod.renew_auth()

    assert result["method"] == "anonymous_ssh_recovery"
    assert result["project_id"] == "project"
    assert saved[0]["refresh_token"] == "recovered-refresh"


if __name__ == "__main__":
    test_initial_enrollment_persists_refresh_credential()
    test_failed_anonymous_refresh_uses_ssh_recovery()
    print("ok: anonymous auth recovery tests passed")
