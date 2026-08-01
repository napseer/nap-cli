import importlib.util
import pathlib


def load_module():
    script_path = pathlib.Path(__file__).resolve().parents[1] / "resources" / "scripts" / "napseer_mcp_server.py"
    spec = importlib.util.spec_from_file_location("napseer_mcp_server_worker_repair_test", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_gateway_worker_repair_separates_credentials_and_returns_no_tokens():
    mod = load_module()
    mod.AUTH = {
        "account_mode": "operator_project",
        "project_slug": "demo",
        "project_name": "Demo",
    }
    mod.DEFAULT_PROJECT_ID = "project-1"
    mod.BASE_URL = "https://api.example.test"
    mod.GATEWAY_AUTH_PATH = pathlib.Path("/definitely/missing/gateway-auth.json")
    mod.ensure_container_uuid = lambda: "container-1"
    mod.normalize_gateway_command = lambda value=None: value or "bash -l"
    written = []
    mod.write_gateway_worker_auth = lambda payload: written.append(dict(payload))

    def request(method, path, payload=None, token_required=True, **kwargs):
        if path == "/v1/service-bootstrap-tokens":
            return {"bootstrap_token": "bootstrap-secret"}
        if path == "/v1/service-registrations":
            assert token_required is False
            return {
                "registration": {"id": "registration-1"},
                "activation_token": "activation-secret",
            }
        if path.endswith("/accept"):
            return {"worker": {"id": "worker-1", "agent_id": "agent-1", "project_id": "project-1"}}
        if path.endswith("/activate"):
            assert token_required is False
            return {
                "worker": {"id": "worker-1", "agent_id": "agent-1", "project_id": "project-1"},
                "token": {
                    "access_token": "worker-access-secret",
                    "refresh_token": "worker-refresh-secret",
                    "expires_at": "later",
                    "refresh_expires_at": "much-later",
                },
            }
        raise AssertionError(path)

    mod.request_json = request
    result = mod.gateway_worker_repair({"display_name": "demo-local-gateway"})

    assert written[0]["account_mode"] == "gateway_worker"
    assert written[0]["token"] == "worker-access-secret"
    assert written[0]["refresh_token"] == "worker-refresh-secret"
    assert "token" not in result
    assert "refresh_token" not in result
    assert result["status"] == "repaired"
