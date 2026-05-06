#!/usr/bin/env python3
"""Smoke-test that legacy local gateway cloud routes stay unavailable."""

import importlib.util
import json
import pathlib
import sys
import urllib.error
import urllib.request


def load_module():
    script_path = pathlib.Path(__file__).resolve().parents[1] / "resources" / "scripts" / "napseer_mcp_server.py"
    sys.path.insert(0, str(script_path.parent))
    spec = importlib.util.spec_from_file_location("napseer_mcp_server", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def request_status(url, method, path):
    data = None
    headers = {}
    if method == "POST":
        data = b"{}"
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(f"{url.rstrip('/')}{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def run():
    mod = load_module()
    mod.AUTH = {"agent_id": "agent-1"}
    mod.LOCAL_UI_ASSETS = {}
    mod.gateway_log = lambda *args, **kwargs: None
    mod.touch_project_agent = lambda: None
    mod.start_project_heartbeat_thread = lambda: None
    mod.start_gateway_log_compactor_thread = lambda: None
    mod.start_gateway_relay_thread = lambda: None
    mod.start_gateway_schedule_thread = lambda: None
    mod.start_gateway_vault_setup_thread = lambda: None
    mod.current_project_id = lambda: "project-1"
    mod.gateway_remote_enabled_value = lambda: False
    mod.read_gateway_relay_secret = lambda: None
    mod.gateway_status = lambda: {"status": "ok", "locked": False}

    result = mod.start_local_ui({"open_browser": False, "port": 0})
    url = result["url"]

    status, body = request_status(url, "GET", "/gateway/status")
    assert status == 200
    assert json.loads(body.decode("utf-8"))["status"] == "ok"

    removed_paths = [
        "/gateway/cloud/connect",
        "/gateway/cloud/input",
        "/gateway/cloud/capture",
        "/gateway/cloud/disconnect",
    ]
    for path in removed_paths:
        status, _ = request_status(url, "POST", path)
        assert status == 404, path

    status, _ = request_status(url, "GET", "/gateway/cloud/ws")
    assert status == 404
    status, _ = request_status(url, "OPTIONS", "/gateway/cloud/connect")
    assert status == 404

    print("ok: local gateway cloud routes are unavailable")


if __name__ == "__main__":
    run()
