#!/usr/bin/env python3
"""Regression tests for auth.json replacement in a running MCP wrapper."""

import importlib.util
import io
import pathlib
import sys
import urllib.error


def load_module():
    script_path = pathlib.Path(__file__).resolve().parents[1] / "resources" / "scripts" / "napseer_mcp_server.py"
    sys.path.insert(0, str(script_path.parent))
    spec = importlib.util.spec_from_file_location("napseer_mcp_server_live_auth_test", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    module.load_project_locator = lambda: None
    module.require_project_locator_service = lambda _locator: None
    return module


class Response:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return b'{"ok":true}'


def auth(token, refresh_token):
    return {
        "base_url": "https://api.example.test",
        "project_id": "project-1",
        "account_mode": "operator_project",
        "oauth_client_id": "nap-cli",
        "token": token,
        "refresh_token": refresh_token,
    }


def run():
    mod = load_module()
    current = auth("fresh-access", "fresh-refresh")
    requests = []
    mod.load_public_auth_file = lambda: dict(current)
    mod.gateway_is_unlocked = lambda: False
    mod.AUTH_PATH = pathlib.Path(__file__)

    # The MCP worker may have started before `nap project attach`, leaving
    # its project selection empty until another process replaces auth.json.
    mod.DEFAULT_PROJECT_ID = None
    assert mod.resolve_project_id({}) == "project-1"

    current = {}
    mod.DEFAULT_PROJECT_ID = "stale-project"
    bootstrap_calls = []

    def bootstrap_project(args):
        bootstrap_calls.append(dict(args))
        mod.DEFAULT_PROJECT_ID = "anonymous-project"
        return {"status": "created", "project_id": "anonymous-project"}

    mod.bootstrap_project = bootstrap_project
    assert mod.resolve_project_id({}) == "anonymous-project"
    assert bootstrap_calls == [
        {
            "slug": mod.default_project_slug(),
            "name": mod.default_project_name(mod.default_project_slug()),
            "description": "Created automatically by the local Napseer MCP runtime.",
            "encryption": "standard",
        }
    ]

    current = {}
    mod.DEFAULT_PROJECT_ID = None

    def limited_project(_args):
        raise mod.SafeToolError(
            "anonymous_space_limit_reached",
            "Anonymous space limit reached. Run `nap project claim` to authenticate and continue.",
            status=403,
            service_code="anonymous_space_limit_reached",
        )

    mod.bootstrap_project = limited_project
    try:
        mod.resolve_project_id({})
    except mod.SafeToolError as exc:
        assert exc.code == "anonymous_space_limit_reached"
        assert exc.service_code == "anonymous_space_limit_reached"
        assert "nap project claim" in exc.safe_message
    else:
        raise AssertionError("anonymous space limit must remain actionable")

    def open_success(request, timeout=None):
        requests.append(request)
        return Response()

    mod.api_urlopen = open_success
    current = auth("fresh-access", "fresh-refresh")
    mod.TOKEN = "stale-access"
    mod.REFRESH_TOKEN = "stale-refresh"
    result = mod.request_json("GET", "/v1/projects/project-1")
    assert result == {"ok": True}
    assert requests[-1].get_header("Authorization") == "Bearer fresh-access"
    assert mod.REFRESH_TOKEN == "fresh-refresh"

    current = auth("stale-access", "stale-refresh")
    requests.clear()
    renew_calls = []
    mod.renew_auth = lambda: renew_calls.append(True)

    def attach_during_request(request, timeout=None):
        requests.append(request)
        if len(requests) == 1:
            current.update(auth("attached-access", "attached-refresh"))
            raise urllib.error.HTTPError(
                request.full_url,
                401,
                "Unauthorized",
                {},
                io.BytesIO(b'{"error":{"code":"unauthorized"}}'),
            )
        return Response()

    mod.api_urlopen = attach_during_request
    result = mod.request_json("GET", "/v1/projects/project-1")
    assert result == {"ok": True}
    assert len(requests) == 2
    assert requests[0].get_header("Authorization") == "Bearer stale-access"
    assert requests[1].get_header("Authorization") == "Bearer attached-access"
    assert renew_calls == []

    print("ok: running MCP wrapper reloads replaced auth state")


if __name__ == "__main__":
    run()
