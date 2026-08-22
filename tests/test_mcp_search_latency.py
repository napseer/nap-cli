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


def indexed_node(path, *, node_type="note", tags=None, status="active", content=""):
    name = path.rsplit("/", 1)[-1]
    return {
        "id": f"node-{path.strip('/').replace('/', '-')}",
        "project_id": "project-1",
        "full_path": path,
        "folder_path": path.rsplit("/", 1)[0] or None,
        "name": name,
        "title": name,
        "type": node_type,
        "tags": tags or [],
        "aliases": [],
        "links": [],
        "metadata": {"status": status},
        "content_text": content,
        "encryption_state": "legacy_plaintext",
        "updated_at": "2026-08-22T00:00:00Z",
    }


def configure_local_index(mod, tmp_path):
    mod.AUTH_DIR = tmp_path
    mod.INDEX_PATH = tmp_path / "index.sqlite"
    mod.INDEX_LOCK_PATH = tmp_path / "index.lock"
    mod.resolve_project_id = lambda _args: "project-1"


def test_single_pass_ranking_prefers_exact_canonical_identity(tmp_path):
    mod = load_module()
    configure_local_index(mod, tmp_path)
    mod.index_node(
        indexed_node(
            "/rules/memory-source-policy",
            node_type="rule",
            tags=["memory", "policy"],
            content="Canonical memory source rule.",
        )
    )
    for index in range(12):
        mod.index_node(
            indexed_node(
                f"/work/current/incidental-{index}",
                content="memory source policy " * (index + 2),
            )
        )

    result = mod.search_local_index({"q": "memory source policy", "limit": 5})

    assert result["items"][0]["full_path"] == "/rules/memory-source-policy"
    assert "exact_identity" in result["items"][0]["match_reason"]
    assert "full_path" in result["items"][0]["matched_fields"]
    assert result["search_strategies"] == [
        {
            "strategy": "single_pass_weighted_candidates",
            "matched": 13,
            "returned": 5,
            "query_count": 1,
            "candidate_limit": 30,
        }
    ]


def test_query_planner_normalizes_plurals_and_reports_intent():
    mod = load_module()

    analysis = mod.analyze_search_query(
        "where did we decide how search queries and policies should work"
    )

    assert "query" in analysis["terms"]
    assert "querie" not in analysis["terms"]
    assert analysis["inferred_intent"] == "decision"
    assert len(mod.fts_query_variants(analysis["raw"])[1]) == 1


def test_local_search_pushes_structured_filters_into_candidate_query(tmp_path):
    mod = load_module()
    configure_local_index(mod, tmp_path)
    nodes = [
        indexed_node(
            "/decisions/search-governance",
            node_type="decision",
            tags=["search"],
            content="search governance",
        ),
        indexed_node(
            "/decisions/old-search-governance",
            node_type="decision",
            tags=["search"],
            status="superseded",
            content="search governance",
        ),
        indexed_node(
            "/plans/search-governance",
            node_type="plan",
            tags=["search"],
            content="search governance",
        ),
        indexed_node(
            "/decisions/other-search",
            node_type="decision",
            tags=["other"],
            content="search governance",
        ),
    ]
    for node in nodes:
        mod.index_node(node)

    result = mod.search_local_index(
        {
            "q": "search governance",
            "folder_path": "/decisions",
            "tag": "search",
            "type": "decision",
            "status": "active",
            "limit": 10,
        }
    )

    assert [item["full_path"] for item in result["items"]] == [
        "/decisions/search-governance"
    ]
    assert result["index"]["query_count"] == 1
    assert set(result["index"]["filters_applied"]) >= {
        "folder_path",
        "tag",
        "type",
        "status",
    }


def test_explicit_intent_boosts_without_filtering(tmp_path):
    mod = load_module()
    configure_local_index(mod, tmp_path)
    mod.index_node(
        indexed_node(
            "/decisions/gateway-relay-authentication",
            node_type="decision",
            content="gateway relay authentication",
        )
    )
    mod.index_node(
        indexed_node(
            "/work/current/gateway-relay-authentication",
            content="gateway relay authentication",
        )
    )

    result = mod.search_local_index(
        {"q": "gateway relay authentication", "intent": "decision", "limit": 10}
    )

    assert result["items"][0]["full_path"].startswith("/decisions/")
    assert len(result["items"]) == 2


def test_complete_filtered_local_search_stays_off_network(tmp_path):
    mod = load_module()
    configure_local_index(mod, tmp_path)
    mod.index_node(
        indexed_node(
            "/documentation/facturacion-electronica",
            tags=["billing"],
            content="facturación electrónica",
        )
    )
    mod.index_node(
        indexed_node(
            "/work/current/facturacion-migration",
            tags=["migration"],
            content="facturación electrónica",
        )
    )
    mod.set_local_graph_index_complete("project-1", True)

    def unexpected_request(*_args, **_kwargs):
        raise AssertionError("a complete filtered local result must not use the network")

    mod.request_json = unexpected_request
    result = mod.search_memory(
        {
            "q": "facturación",
            "tag": "billing",
            "limit": 1,
            "view": "summary",
        }
    )

    assert result["sources"] == ["local"]
    assert [item["full_path"] for item in result["items"]] == [
        "/documentation/facturacion-electronica"
    ]


def test_discovery_schema_documents_dynamic_sort_and_intent():
    mod = load_module()
    tool = next(item for item in mod.tools() if item["name"] == "nap_discover")
    properties = tool["inputSchema"]["properties"]

    assert properties["intent"]["enum"] == [
        "any",
        "decision",
        "rule",
        "plan",
        "task",
        "implementation",
        "documentation",
    ]
    assert "default" not in properties["sort"]
    assert "relevance" in properties["sort"]["description"]
