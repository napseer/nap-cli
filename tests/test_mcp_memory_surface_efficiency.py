#!/usr/bin/env python3
"""Contract tests for the reduced, bounded agent-memory surface."""

import importlib.util
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
    module.CONFIGURED_TOOL_PROFILES = None
    return module


def test_default_surface_is_exactly_the_core_fourteen(mod):
    assert [tool["name"] for tool in mod.tools()] == [
        "nap_apropos", "nap_man", "nap_doctor", "nap_whoami",
        "nap_discover", "nap_context", "nap_node_by_path", "nap_node_get",
        "nap_create_node", "nap_node_patch", "nap_bulk", "nap_ln",
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

    node_get = by_name["nap_node_get"]["inputSchema"]["properties"]
    assert node_get["view"]["enum"] == ["content", "outline", "edit"]
    assert node_get["view"]["default"] == "content"


def test_tool_text_does_not_duplicate_large_structured_payload(mod):
    payload = {"ok": True, "items": [{"content_text": "x" * 20_000}]}
    result = mod.tool_result(payload)
    assert result["structuredContent"] == payload
    assert len(result["content"][0]["text"]) < 200
    assert "20000" not in result["content"][0]["text"]


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
