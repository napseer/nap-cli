#!/usr/bin/env python3
"""Regression tests for account-login -> project-create authorization."""

import importlib.util
import pathlib
import sys


PROJECT_ID = "33333333-3333-3333-3333-333333333333"


def load_module():
    script_path = pathlib.Path(__file__).resolve().parents[1] / "resources" / "scripts" / "napseer_mcp_server.py"
    sys.path.insert(0, str(script_path.parent))
    spec = importlib.util.spec_from_file_location("napseer_mcp_server_project_create_oauth_test", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def configure_account_state(mod, scope):
    mod.AUTH = {
        "account_mode": "operator_account",
        "oauth_scope": scope,
    }
    mod.TOKEN = "test-token"
    mod.BASE_URL = "https://api.example.test"
    mod.ensure_enrolled = lambda args: {"status": "already_enrolled"}
    mod.refresh_public_auth_state = lambda: mod.AUTH
    mod.register_project_signing_key = lambda args: None


def project_args():
    return {
        "slug": "created-project",
        "name": "Created Project",
        "description": "",
    }


def test_account_login_requests_create_and_runtime_scopes():
    mod = load_module()
    captured = []
    saves = []
    mod.AUTH_PATH = pathlib.Path("/tmp/napseer-test/auth.json")
    mod.oauth_loopback_authorize = lambda args: (
        captured.append(dict(args))
        or {
            "api_base_url": "https://api.example.test",
            "access_token": "test-token",
            "refresh_token": "test-refresh-token",
            "expires_in": 3600,
            "scope": args["scope"],
        }
    )
    mod.replace_public_auth_state = lambda updates, clear_keys=(): saves.append(dict(updates))

    result = mod.operator_account_login({"open_browser": False})

    requested = set(captured[0]["scope"].split())
    assert "napseer.projects.write" in requested
    assert set(mod.LOCAL_PROJECT_OAUTH_SCOPE.split()).issubset(requested)
    assert saves[0]["account_mode"] == "operator_account"
    assert "nap project create" in result["message"]


def test_legacy_account_scope_fails_before_http_with_recovery():
    mod = load_module()
    configure_account_state(mod, "openid profile email napseer.projects.read")
    requests = []
    mod.request_json = lambda *args, **kwargs: requests.append((args, kwargs))

    try:
        mod.create_project_with_state(project_args())
    except mod.SafeToolError as exc:
        assert exc.code == "project_create_scope_required"
        assert exc.status == 403
        assert "nap auth login" in exc.safe_message
    else:
        raise AssertionError("legacy account scope should require a new login")

    assert not requests


def test_successful_create_transitions_local_state_to_project_mode():
    mod = load_module()
    configure_account_state(mod, mod.OPERATOR_ACCOUNT_OAUTH_SCOPE)
    saves = []
    mod.request_json = lambda method, path, payload=None: {
        "id": PROJECT_ID,
        "slug": payload["slug"],
        "name": payload["name"],
    }
    mod.save_auth = lambda updates: saves.append(dict(updates))

    project, status, _message = mod.create_project_with_state(project_args())

    assert status == "created"
    assert project["id"] == PROJECT_ID
    assert saves[0]["project_id"] == PROJECT_ID
    assert saves[0]["account_mode"] == "operator_project"


def test_server_scope_denial_is_actionable():
    mod = load_module()
    configure_account_state(mod, mod.OPERATOR_ACCOUNT_OAUTH_SCOPE)

    def denied(*args, **kwargs):
        raise mod.SafeToolError(
            "http_403",
            "Napseer POST request failed with HTTP 403.",
            status=403,
            service_code="insufficient_scope",
        )

    mod.request_json = denied

    try:
        mod.create_project_with_state(project_args())
    except mod.SafeToolError as exc:
        assert exc.code == "project_create_scope_required"
        assert "nap auth login" in exc.safe_message
    else:
        raise AssertionError("scope denial should return an actionable error")


if __name__ == "__main__":
    test_account_login_requests_create_and_runtime_scopes()
    test_legacy_account_scope_fails_before_http_with_recovery()
    test_successful_create_transitions_local_state_to_project_mode()
    test_server_scope_denial_is_actionable()
    print("ok: project create OAuth lifecycle passed")
