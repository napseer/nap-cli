#!/usr/bin/env python3
"""Smoke-test existing-project OAuth attachment for folder bootstrap."""

import importlib.util
import pathlib
import sys
import tempfile


PROJECT_ID = "11111111-1111-1111-1111-111111111111"


def load_module():
    script_path = pathlib.Path(__file__).resolve().parents[1] / "resources" / "scripts" / "napseer_mcp_server.py"
    sys.path.insert(0, str(script_path.parent))
    spec = importlib.util.spec_from_file_location("napseer_mcp_server_project_attach_test", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def run():
    mod = load_module()
    test_state = tempfile.TemporaryDirectory()
    mod.AUTH_PATH = pathlib.Path(test_state.name) / "auth.json"
    saves = []

    def save_auth(updates):
        saves.append(dict(updates))

    mod.oauth_loopback_authorize = lambda args: {
        "api_base_url": "https://api.example.test",
        "access_token": "oauth-token",
        "refresh_token": "oauth-refresh-token",
        "refresh_expires_at": "2099-01-01T00:00:00Z",
        "expires_in": 3600,
        "scope": args["scope"],
        "project_id": PROJECT_ID,
    }
    mod.save_auth = save_auth
    mod.save_auth_file_credentials = save_auth
    mod.replace_public_auth_state = lambda updates, clear_keys=(): save_auth(updates)
    mod.request_json = lambda method, path: {
        "id": PROJECT_ID,
        "slug": "existing-project",
        "name": "Existing Project",
        "encryption_state": "plaintext",
    }

    result = mod.operator_project_attach({"open_browser": False})

    assert saves[0]["token"] == "oauth-token"
    assert saves[0]["refresh_token"] == "oauth-refresh-token"
    assert saves[0]["project_id"] == PROJECT_ID
    assert saves[0]["account_mode"] == "operator_project"
    assert "napseer.projects.read" in saves[0]["oauth_scope"]
    assert saves[1]["project_slug"] == "existing-project"
    assert result["project_id"] == PROJECT_ID
    assert result["project"]["name"] == "Existing Project"

    saves.clear()
    mod.oauth_loopback_authorize = lambda args: {
        "api_base_url": "https://api.example.test",
        "access_token": "account-token",
        "refresh_token": "account-refresh-token",
        "refresh_expires_at": "2099-01-01T00:00:00Z",
        "expires_in": 3600,
        "scope": args["scope"],
    }
    result = mod.operator_account_login({"open_browser": False})
    assert saves[0]["refresh_token"] == "account-refresh-token"
    assert saves[0]["account_mode"] == "operator_account"
    assert "project_id" not in saves[0]
    assert result["mode"] == "operator_account"

    print("ok: project attach OAuth smoke passed")


if __name__ == "__main__":
    run()
