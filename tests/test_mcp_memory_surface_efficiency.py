#!/usr/bin/env python3
"""Contract tests for the reduced, bounded agent-memory surface."""

import importlib.util
import json
import pathlib
import sys
import tempfile

import pytest


def load_module():
    script_path = pathlib.Path(__file__).resolve().parents[1] / "resources" / "scripts" / "napseer_mcp_server.py"
    sys.path.insert(0, str(script_path.parent))
    spec = importlib.util.spec_from_file_location("napseer_mcp_server_surface_test", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    module.DEFAULT_PROJECT_ID = "project-1"
    return module


@pytest.fixture
def mod(monkeypatch):
    monkeypatch.delenv("NAPSEER_TOOL_PROFILES", raising=False)
    module = load_module()
    module._auth_state = tempfile.TemporaryDirectory()
    module.AUTH_PATH = pathlib.Path(module._auth_state.name) / "auth.json"
    module.AUTH = {"account_id": "acct-1", "token": "token"}
    module.CONFIGURED_TOOL_PROFILES = None
    return module


def test_default_surface_is_exactly_the_core_fifteen(mod):
    assert [tool["name"] for tool in mod.tools()] == [
        "nap_apropos", "nap_man", "nap_doctor", "nap_whoami",
        "nap_discover", "nap_context", "nap_node_by_path", "nap_node_get",
        "nap_create_node", "nap_node_patch", "nap_bulk", "nap_batch", "nap_ln",
        "nap_mv", "nap_rm",
    ]


def test_profiles_expand_surface_without_restoring_pruned_tools(mod, monkeypatch):
    monkeypatch.setenv("NAPSEER_TOOL_PROFILES", "workflow,maintenance")
    names = {tool["name"] for tool in mod.tools()}
    assert {"nap_plan_list_active", "nap_kanban_complete", "nap_index_status"} <= names
    assert not ({"nap_recent", "nap_find_related", "nap_backlinks", "nap_ls_folders", "nap_ls_tags"} & names)


def test_apropos_can_describe_inactive_profile_tools(mod):
    result = mod.explore_tools({"q": "kanban complete"})
    card = result["groups"]["kanban"][0]
    assert card["name"] == "nap_kanban_complete"
    assert card["enabled"] is False
    assert card["profile"] == "workflow"


def test_pruned_and_profile_gated_calls_are_actionable(mod):
    with pytest.raises(mod.SafeToolError) as removed:
        mod.call_tool_impl("nap_recent", {})
    assert removed.value.code == "tool_removed"
    assert "nap_discover" in removed.value.safe_message

    with pytest.raises(mod.SafeToolError) as gated:
        mod.call_tool_impl("nap_kanban_list", {})
    assert gated.value.code == "tool_profile_required"
    assert "workflow" in gated.value.safe_message


def test_context_and_node_read_schemas_are_bounded(mod):
    by_name = {tool["name"]: tool for tool in mod.all_tools()}
    context = by_name["nap_context"]["inputSchema"]["properties"]
    assert context["depth"]["maximum"] == 3
    assert context["direction"]["enum"] == ["outgoing", "incoming", "both"]
    assert context["mode"]["enum"] == ["hybrid", "local", "server"]
    assert "relations" in context
    assert "max_edges" in context
    assert "max_per_level" in context
    assert context["node_ids"]["maxItems"] == 10
    assert context["paths"]["maxItems"] == 10
    assert context["uris"]["maxItems"] == 10

    node_get = by_name["nap_node_get"]["inputSchema"]["properties"]
    assert node_get["view"]["enum"] == ["content", "outline", "edit"]
    assert node_get["view"]["default"] == "content"


def test_tool_text_does_not_duplicate_large_structured_payload(mod):
    payload = {"ok": True, "items": [{"content_text": "x" * 20_000}]}
    result = mod.tool_result(payload)
    assert result["structuredContent"] == payload
    assert len(result["content"][0]["text"]) < 2_000
    assert "20000" not in result["content"][0]["text"]


def test_tool_text_keeps_small_structured_results_useful_for_text_only_clients(mod):
    payload = {
        "status": "current",
        "full_path": "/docs/useful",
        "view": "content",
        "content_text": "A small useful body.",
    }

    result = mod.tool_result(payload)

    assert json.loads(result["content"][0]["text"]) == payload


def test_large_graph_text_fallback_contains_bounded_node_and_edge_previews(mod):
    payload = {
        "ok": True,
        "nodes": [
            {"id": f"node-{index}", "full_path": f"/docs/{index}", "title": f"Node {index}"}
            for index in range(100)
        ],
        "edges": [
            {"source": f"node-{index}", "target": f"node-{index + 1}", "relation": "references"}
            for index in range(99)
        ],
        "warnings": ["Use first-class links."],
    }

    fallback = json.loads(mod.tool_result(payload)["content"][0]["text"])

    assert fallback["nodes_count"] == 100
    assert fallback["edges_count"] == 99
    assert len(fallback["nodes_preview"]) == 5
    assert len(fallback["edges_preview"]) == 5
    assert fallback["warnings"] == ["Use first-class links."]
    assert fallback["structured_result"] is True


def test_local_context_returns_title_radius_without_node_bodies(mod):
    state = tempfile.TemporaryDirectory()
    mod._surface_state = state
    mod.AUTH_DIR = pathlib.Path(state.name)
    mod.INDEX_PATH = mod.AUTH_DIR / "index.sqlite"
    mod.INDEX_LOCK_PATH = mod.AUTH_DIR / "index.sqlite.lock"
    root = {
        "id": "root", "project_id": "project-1", "full_path": "/docs/root",
        "folder_path": "/docs", "name": "root", "type": "note",
        "metadata": {"title": "Root", "status": "active"}, "tags": [],
        "links": [{"path": "/docs/child", "relation": "explains"}],
        "content_text": "secret body", "updated_at": "2026-08-16T12:00:00Z",
    }
    child = {
        "id": "child", "project_id": "project-1", "full_path": "/docs/child",
        "folder_path": "/docs", "name": "child", "type": "note",
        "metadata": {"title": "Child", "status": "active"}, "tags": [],
        "links": [], "content_text": "another body", "updated_at": "2026-08-16T12:01:00Z",
    }
    mod.index_node(root)
    mod.index_node(child)

    result = mod.get_memory_context({
        "node_id": "root", "mode": "local", "depth": 2,
        "direction": "both", "relations": ["explains"],
    })
    assert [(item["title"], item["depth"]) for item in result["nodes"]] == [("Root", 0), ("Child", 1)]
    assert result["edges"] == [{"source": "root", "target": "child", "relation": "explains"}]
    assert result["levels"] == [
        {"depth": 0, "node_ids": ["root"]},
        {"depth": 1, "node_ids": ["child"]},
    ]
    assert "content_text" not in result["nodes"][0]
    assert result["source"] == "local_index"


def test_local_context_reports_partial_and_total_missing_seeds_actionably(mod):
    state = tempfile.TemporaryDirectory()
    mod._surface_missing_seed_state = state
    mod.AUTH_DIR = pathlib.Path(state.name)
    mod.INDEX_PATH = mod.AUTH_DIR / "index.sqlite"
    mod.INDEX_LOCK_PATH = mod.AUTH_DIR / "index.sqlite.lock"
    mod.index_node({
        "id": "root", "project_id": "project-1", "full_path": "/docs/root",
        "folder_path": "/docs", "name": "root", "type": "note",
        "metadata": {"title": "Root"}, "tags": [], "links": [],
        "content_text": "body", "updated_at": "2026-08-16T12:00:00Z",
    })

    partial = mod.get_memory_context({
        "node_ids": ["root", "missing"], "mode": "local", "depth": 0,
    })

    assert partial["requested_seed_node_ids"] == ["root", "missing"]
    assert partial["seed_node_ids"] == ["root"]
    assert partial["missing_seed_node_ids"] == ["missing"]
    assert any("active project" in warning for warning in partial["warnings"])

    with pytest.raises(mod.SafeToolError) as missing:
        mod.get_memory_context({"node_id": "missing", "mode": "local"})
    assert missing.value.code == "context_seed_not_found"
    assert "nap_whoami" in missing.value.safe_message
    assert "nap_discover" in missing.value.safe_message


def test_context_translates_server_not_found_into_seed_diagnostic(mod):
    def missing_request(*_args, **_kwargs):
        raise mod.SafeToolError(
            "http_404",
            "Napseer GET request failed with HTTP 404.",
            status=404,
            service_code="not_found",
        )

    mod.request_json = missing_request

    with pytest.raises(mod.SafeToolError) as missing:
        mod.get_memory_context({"node_id": "missing", "mode": "server"})

    assert missing.value.code == "context_seed_not_found"
    assert missing.value.status == 404
    assert mod.safe_exception_payload(missing.value)["service_code"] == "not_found"


def test_context_rejects_cross_project_uri_before_graph_reads(mod):
    with pytest.raises(mod.SafeToolError) as mismatch:
        mod.get_memory_context({
            "uri": "nap://other-project/project-2/node-1",
            "mode": "server",
        })

    assert mismatch.value.code == "context_project_mismatch"
    assert "nap_whoami" in mismatch.value.safe_message


def test_local_context_projects_requested_view_and_bounds_frontier(mod):
    state = tempfile.TemporaryDirectory()
    mod._surface_view_state = state
    mod.AUTH_DIR = pathlib.Path(state.name)
    mod.INDEX_PATH = mod.AUTH_DIR / "index.sqlite"
    mod.INDEX_LOCK_PATH = mod.AUTH_DIR / "index.sqlite.lock"
    children = []
    for index in range(5):
        children.append({
            "id": f"child-{index}", "project_id": "project-1",
            "full_path": f"/docs/child-{index}", "folder_path": "/docs",
            "name": f"child-{index}", "type": "note", "metadata": {},
            "tags": [], "links": [], "content_text": "",
            "updated_at": f"2026-08-16T12:0{index + 1}:00Z",
        })
    mod.index_node({
        "id": "root", "project_id": "project-1", "full_path": "/docs/root",
        "folder_path": "/docs", "name": "root", "type": "note",
        "metadata": {"title": "Root"}, "tags": [],
        "links": [
            {"node_id": child["id"], "relation": "references"}
            for child in children
        ],
        "content_text": "", "updated_at": "2026-08-16T12:00:00Z",
    })
    for child in children:
        mod.index_node(child)

    result = mod.get_memory_context({
        "node_id": "root", "mode": "local", "depth": 1,
        "max_nodes": 2, "max_per_level": 1, "view": "paths",
    })

    assert result["view"] == "paths"
    assert "title" not in result["nodes"][0]
    assert set(result["nodes"][0]) == {
        "id", "full_path", "type", "status", "updated_at", "depth"
    }
    assert len(result["frontier"]) <= 1
    assert "max_frontier" in result["truncation_reasons"]
    assert result["node_count"] == 2
    assert result["edge_count"] == 1


def test_context_explains_empty_first_class_graph(mod):
    state = tempfile.TemporaryDirectory()
    mod._surface_empty_graph_state = state
    mod.AUTH_DIR = pathlib.Path(state.name)
    mod.INDEX_PATH = mod.AUTH_DIR / "index.sqlite"
    mod.INDEX_LOCK_PATH = mod.AUTH_DIR / "index.sqlite.lock"
    mod.index_node({
        "id": "root", "project_id": "project-1", "full_path": "/docs/root",
        "folder_path": "/docs", "name": "root", "type": "note",
        "metadata": {"title": "Root"}, "tags": [], "links": [],
        "content_text": "mentions /docs/other only in prose",
        "updated_at": "2026-08-16T12:00:00Z",
    })

    result = mod.get_memory_context({"node_id": "root", "mode": "local", "depth": 1})

    assert any("Prose mentions are not traversed" in warning for warning in result["warnings"])
    assert "nap_ln" in result["next_action"]


def test_hybrid_context_falls_back_until_local_graph_coverage_is_complete(mod):
    state = tempfile.TemporaryDirectory()
    mod._surface_fallback_state = state
    mod.AUTH_DIR = pathlib.Path(state.name)
    mod.INDEX_PATH = mod.AUTH_DIR / "index.sqlite"
    mod.INDEX_LOCK_PATH = mod.AUTH_DIR / "index.sqlite.lock"
    mod.index_node({
        "id": "root", "project_id": "project-1", "full_path": "/docs/root",
        "folder_path": "/docs", "name": "root", "type": "note",
        "metadata": {"title": "Root"}, "tags": [], "links": [],
        "content_text": "body", "updated_at": "2026-08-16T12:00:00Z",
    })
    calls = []

    def server_context(_args, seed_ids):
        calls.append(seed_ids)
        return {"ok": True, "nodes": [{"id": "root"}], "edges": [], "source": "server"}

    mod.server_memory_context = server_context

    result = mod.get_memory_context({"node_id": "root", "mode": "hybrid"})

    assert result["source"] == "server"
    assert result["local_fallback"]
    assert calls == [["root"]]


def test_local_context_does_not_emit_edges_beyond_requested_depth(mod):
    state = tempfile.TemporaryDirectory()
    mod._surface_depth_state = state
    mod.AUTH_DIR = pathlib.Path(state.name)
    mod.INDEX_PATH = mod.AUTH_DIR / "index.sqlite"
    mod.INDEX_LOCK_PATH = mod.AUTH_DIR / "index.sqlite.lock"
    nodes = [
        {
            "id": "root", "project_id": "project-1", "full_path": "/docs/root",
            "folder_path": "/docs", "name": "root", "type": "note", "metadata": {},
            "tags": [], "links": [
                {"node_id": "left", "relation": "references"},
                {"node_id": "right", "relation": "references"},
            ], "content_text": "", "updated_at": "2026-08-16T12:00:00Z",
        },
        {
            "id": "left", "project_id": "project-1", "full_path": "/docs/left",
            "folder_path": "/docs", "name": "left", "type": "note", "metadata": {},
            "tags": [], "links": [{"node_id": "right", "relation": "explains"}],
            "content_text": "", "updated_at": "2026-08-16T12:01:00Z",
        },
        {
            "id": "right", "project_id": "project-1", "full_path": "/docs/right",
            "folder_path": "/docs", "name": "right", "type": "note", "metadata": {},
            "tags": [], "links": [], "content_text": "",
            "updated_at": "2026-08-16T12:02:00Z",
        },
    ]
    for item in nodes:
        mod.index_node(item)

    result = mod.get_memory_context({
        "node_id": "root", "mode": "local", "depth": 1, "direction": "both",
    })

    assert {item["id"] for item in result["nodes"]} == {"root", "left", "right"}
    assert result["edges"] == [
        {"source": "root", "target": "left", "relation": "references"},
        {"source": "root", "target": "right", "relation": "references"},
    ]


def test_index_sync_upgrades_pre_graph_index_with_full_reindex(mod):
    state = tempfile.TemporaryDirectory()
    mod._surface_upgrade_state = state
    mod.AUTH_DIR = pathlib.Path(state.name)
    mod.INDEX_PATH = mod.AUTH_DIR / "index.sqlite"
    mod.INDEX_LOCK_PATH = mod.AUTH_DIR / "index.sqlite.lock"
    mod.index_node({
        "id": "old", "project_id": "project-1", "full_path": "/docs/old",
        "folder_path": "/docs", "name": "old", "type": "note",
        "metadata": {}, "tags": [], "links": [], "content_text": "old",
        "updated_at": "2026-08-16T12:00:00Z",
    })
    fresh = {
        "id": "fresh", "project_id": "project-1", "full_path": "/docs/fresh",
        "folder_path": "/docs", "name": "fresh", "type": "note",
        "metadata": {}, "tags": [],
        "links": [{"node_id": "target", "relation": "references"}],
        "content_text": "fresh", "updated_at": "2026-08-16T12:01:00Z",
    }
    mod.request_json = lambda *_args, **_kwargs: {"items": [fresh], "next_cursor": None}

    result = mod.sync_local_index({})

    assert result["mode"] == "full_reindex"
    assert result["reason"] == "local_graph_index_incomplete"
    assert result["graph_complete"] is True
    assert result["index"]["graph_complete"] is True
    with mod.index_connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM local_index_edges WHERE project_id = ?",
            ("project-1",),
        ).fetchone()[0] == 1


def test_discovery_sort_and_returned_item_facets(mod):
    mod.list_project_nodes = lambda args: {
        "items": [
            {"name": "Beta", "folder_path": "/b", "tags": ["x"], "type": "note", "status": "active", "updated_at": "2026-01-01T00:00:00Z"},
            {"name": "Alpha", "folder_path": "/a", "tags": ["x", "y"], "type": "plan", "status": "planned", "updated_at": "2026-02-01T00:00:00Z"},
        ]
    }
    result = mod.discover_memory({"sort": "title_asc", "facets": ["folder", "tag", "status"]})
    assert [item["name"] for item in result["items"]] == ["Alpha", "Beta"]
    assert result["facets"]["scope"] == "returned_items"
    assert result["facets"]["counts"]["tag"][0] == {"value": "x", "count": 2}


def test_node_get_content_and_outline_hide_mutation_envelopes(mod):
    node = {
        "id": "node-1", "project_id": "project-1", "full_path": "/docs/one",
        "name": "one", "type": "note", "metadata": {"title": "One", "status": "active"},
        "links": [], "content_text": "# First\nBody\n## Second\nMore",
        "encrypted_content_envelope": {"ciphertext": "not-for-read-view"},
        "updated_at": "2026-08-16T12:00:00Z",
    }
    mod.get_node_by_id = lambda args: node
    content = mod.node_get({"node_id": "node-1"})
    outline = mod.node_get({"node_id": "node-1", "view": "outline"})
    assert content["content_text"].startswith("# First")
    assert "encrypted_content_envelope" not in content
    assert [heading["title"] for heading in outline["headings"]] == ["First", "Second"]
    assert "content_text" not in outline
