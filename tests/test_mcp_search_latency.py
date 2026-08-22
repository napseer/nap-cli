import importlib.util
import pathlib
import sys
import time


def load_module():
    script_path = (
        pathlib.Path(__file__).resolve().parents[1]
        / "resources"
        / "scripts"
        / "napseer_mcp_server.py"
    )
    sys.path.insert(0, str(script_path.parent))
    spec = importlib.util.spec_from_file_location(
        "napseer_mcp_server_search_latency_test", script_path
    )
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_natural_language_discovery_uses_one_backend_search_request(tmp_path):
    mod = load_module()
    calls = []
    mod.INDEX_PATH = tmp_path / "missing-index.sqlite"
    mod.resolve_project_id = lambda _args: "project-1"
    mod.project_encryption_state = lambda _project_id: "standard"

    def request(method, path, payload=None, **_kwargs):
        calls.append((method, path, payload))
        return {"items": [], "next_cursor": None, "query_analysis": {"mode": "terms"}}

    mod.request_json = request

    result = mod.discover_memory(
        {
            "q": "SIARA signer facturacion certificate API XML signature",
            "limit": 20,
            "view": "summary",
        }
    )

    assert result["ok"] is True
    assert len(calls) == 1
    assert calls[0][0] == "GET"
    assert "/nodes?" in calls[0][1]


def test_sparse_index_search_latency_does_not_scale_with_query_variants(tmp_path):
    mod = load_module()
    calls = []
    mod.INDEX_PATH = tmp_path / "missing-index.sqlite"
    mod.resolve_project_id = lambda _args: "project-1"
    mod.project_encryption_state = lambda _project_id: "standard"

    def request(_method, _path, _payload=None, **_kwargs):
        calls.append(time.monotonic())
        time.sleep(0.03)
        return {"items": [], "next_cursor": None}

    mod.request_json = request
    started = time.monotonic()

    mod.search_memory(
        {
            "q": "one two three four five six seven eight",
            "limit": 20,
            "view": "summary",
        }
    )

    elapsed = time.monotonic() - started
    assert len(calls) == 1
    assert elapsed < 0.15


def test_complete_local_search_skips_remote_encryption_preflight_and_search(tmp_path):
    mod = load_module()
    mod.INDEX_PATH = tmp_path / "index.sqlite"
    mod.INDEX_PATH.touch()
    mod.resolve_project_id = lambda _args: "project-1"
    mod.project_encryption_state = lambda _project_id: "unknown"
    items = [
        {
            "id": f"node-{index}",
            "project_id": "project-1",
            "full_path": f"/notes/result-{index}",
            "name": f"result-{index}",
            "type": "note",
            "tags": [],
            "updated_at": "2026-08-22T00:00:00Z",
        }
        for index in range(5)
    ]
    mod.local_index_diagnostics = lambda _project_id: {
        "exists": True,
        "graph_complete": True,
    }
    mod.search_local_index = lambda _args: {
        "items": items,
        "query": {"terms": ["result"]},
        "search_strategies": [],
        "index": {"exists": True, "graph_complete": True},
    }

    def unexpected_request(*_args, **_kwargs):
        raise AssertionError("complete local search must not make an HTTP request")

    mod.request_json = unexpected_request
    result = mod.search_memory({"q": "result", "limit": 5, "view": "summary"})

    assert result["sources"] == ["local"]
    assert len(result["items"]) == 5
    assert all(item["node_id"].startswith("node-") for item in result["items"])
