#!/usr/bin/env python3
"""Smoke-test active plan, kanban, and plan lifecycle MCP helpers."""

import importlib.util
import pathlib
import sys

import pytest


def load_module():
    script_path = pathlib.Path(__file__).resolve().parents[1] / "resources" / "scripts" / "napseer_mcp_server.py"
    sys.path.insert(0, str(script_path.parent))
    spec = importlib.util.spec_from_file_location("napseer_mcp_server_ergonomics_test", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    module.AUTH = {"account_id": "acct-1", "token": "token", "token_expires_at": "later"}
    module.DEFAULT_PROJECT_ID = "project-1"
    module.project_memory_encryption_active = lambda project_id: False
    return module


@pytest.fixture
def mod():
    return load_module()


def node(path, status="planned", metadata=None, tags=None, content="body text", archived_at=None, node_type="plan", links=None):
    folder, name = path.rsplit("/", 1)
    merged_metadata = {"status": status}
    if metadata:
        merged_metadata.update(metadata)
    return {
        "id": f"id-{name}",
        "project_id": "project-1",
        "full_path": path,
        "folder_path": folder,
        "name": name,
        "type": node_type,
        "tags": tags or [],
        "links": links or [],
        "metadata": merged_metadata,
        "content_text": content,
        "updated_at": "2026-05-06T12:00:00Z",
        "archived_at": archived_at,
        "encryption_state": "plaintext",
    }


def test_active_plan_listing(mod):
    active = node("/plans/active", "in_progress", tags=["plan"])
    completed = node("/plans/completed", "completed", tags=["plan", "completed"])
    archived = node("/plans/archived", "planned", tags=["plan"], archived_at="2026-05-06T12:01:00Z")

    def fake_request(method, path, payload=None, **kwargs):
        assert method == "GET"
        assert "/nodes?" in path
        assert "folder_path=%2Fplans" in path
        return {"items": [active, completed, archived], "next_cursor": None}

    mod.request_json = fake_request
    result = mod.list_active_plans({})
    assert result["ok"] is True
    assert result["view"] == "summary"
    assert [item["full_path"] for item in result["items"]] == ["/plans/active"]
    assert "content_text" not in result["items"][0]
    assert result["items"][0]["status"] == "in_progress"


def test_node_discovery_allows_large_requested_limit(mod):
    calls = []

    def fake_request(method, path, payload=None, **kwargs):
        calls.append(path)
        assert method == "GET"
        assert "limit=250" in path
        return {"items": [node(f"/plans/item-{index}") for index in range(250)], "next_cursor": None}

    mod.request_json = fake_request
    result = mod.list_project_nodes({"folder_path": "/plans", "limit": 250})
    assert result["ok"] is True
    assert len(result["items"]) == 250
    assert result["budget"]["limit"] == 250
    assert result["budget"]["max_limit"] > 200
    assert "content_text" not in result["items"][0]
    assert len(calls) == 1


def test_node_by_path_returns_canonical_identity_uri(mod):
    mod.AUTH["project_slug"] = "napseer"
    found = node("/documentation/product/prd", "active", node_type="product_spec")
    found["id"] = "node-123"

    def fake_request(method, path, payload=None, **kwargs):
        assert method == "GET"
        assert "/nodes/by-path?" in path
        assert "path=%2Fdocumentation%2Fproduct%2Fprd" in path
        assert "view=render" not in path
        return {
            "identity": {
                "node_id": "node-123",
                "canonical_uri": "nap://napseer/project-1/node-123",
                "project_id": "project-1",
                "project_name": "napseer",
                "lookup_kind": "path",
                "lookup_value": "/documentation/product/prd",
                "legacy_full_path": "/documentation/product/prd",
            },
            "route": {
                "id": "node-123",
                "project_id": "project-1",
                "full_path": "/documentation/product/prd",
                "type": "product_spec",
                "name": "prd",
                "folder_path": "/documentation/product",
            },
            "warnings": ["Path lookup is index/route resolution only."],
        }

    mod.request_json = fake_request
    result = mod.node_by_path({"path": "documentation/product/prd"})
    assert result["ok"] is True
    assert result["status"] == "resolved"
    assert result["identity"] == {
        "node_id": "node-123",
        "canonical_uri": "nap://napseer/project-1/node-123",
        "project_id": "project-1",
        "project_name": "napseer",
        "lookup_kind": "path",
        "lookup_value": "/documentation/product/prd",
        "legacy_full_path": "/documentation/product/prd",
    }
    assert result["route"]["full_path"] == "/documentation/product/prd"
    assert "content_text" not in result["route"]
    assert "preview" not in result["route"]
    assert "read_fingerprint" not in result["route"]
    assert "Path lookup is index/route resolution only" in result["warnings"][0]


def test_path_resolver_then_read_uses_node_id_route(mod):
    calls = []

    def fake_request(method, path, payload=None, **kwargs):
        calls.append(path)
        assert method == "GET"
        if "/nodes/by-path?" in path:
            assert "view=" not in path
            return {
                "identity": {"node_id": "node-123", "project_id": "project-1"},
                "route": {"id": "node-123", "project_id": "project-1", "full_path": "/documentation/product/prd"},
            }
        assert path == "/v1/projects/project-1/nodes/node-123"
        return {**node("/documentation/product/prd", "active", node_type="product_spec"), "id": "node-123"}

    mod.request_json = fake_request
    result = mod.read_node_by_path({"path": "/documentation/product/prd"})
    assert result["id"] == "node-123"
    assert calls == [
        "/v1/projects/project-1/nodes/by-path?path=%2Fdocumentation%2Fproduct%2Fprd",
        "/v1/projects/project-1/nodes/node-123",
    ]


def test_node_by_id_returns_canonical_identity_uri(mod):
    mod.AUTH["project_slug"] = "napseer"
    found = node("/documentation/product/prd", "active", node_type="product_spec")
    found["id"] = "node-123"

    def fake_request(method, path, payload=None, **kwargs):
        assert method == "GET"
        assert path == "/v1/projects/project-1/nodes/node-123"
        return found

    mod.request_json = fake_request
    result = mod.node_by_id({"node_id": "node-123"})
    assert result["ok"] is True
    assert result["identity"]["lookup_kind"] == "node_id"
    assert result["identity"]["canonical_uri"] == "nap://napseer/project-1/node-123"
    assert result["node"]["id"] == "node-123"


def test_node_by_uri_resolves_canonical_uri(mod):
    mod.AUTH["project_slug"] = "napseer"
    found = node("/documentation/product/prd", "active", node_type="product_spec")
    found["id"] = "node-123"

    def fake_request(method, path, payload=None, **kwargs):
        assert method == "GET"
        assert path == "/v1/projects/project-1/nodes/node-123"
        return found

    mod.request_json = fake_request
    result = mod.node_by_uri({"uri": "nap://napseer/project-1/node-123"})
    assert result["ok"] is True
    assert result["identity"]["lookup_kind"] == "uri"
    assert result["identity"]["lookup_value"] == "nap://napseer/project-1/node-123"
    assert result["identity"]["canonical_uri"] == "nap://napseer/project-1/node-123"


def test_node_get_accepts_node_id_without_path(mod):
    found = node("/documentation/product/prd", "active", node_type="product_spec")
    found["id"] = "node-123"

    def fake_request(method, path, payload=None, **kwargs):
        assert method == "GET"
        assert path == "/v1/projects/project-1/nodes/node-123"
        return found

    mod.request_json = fake_request
    result = mod.node_get({"node_id": "node-123", "view": "render"})
    assert result["id"] == "node-123"
    assert result["full_path"] == "/documentation/product/prd"


def test_node_get_edit_view_uses_id_route_not_by_path(mod):
    edit_payload = {
        "node": node("/documentation/product/prd", "active", node_type="product_spec"),
        "revision": "2026-05-06T12:00:00Z",
        "read_fingerprint": "fingerprint-1",
        "content_lines": ["body text"],
        "line_count": 1,
        "ends_with_newline": False,
    }
    edit_payload["node"]["id"] = "node-123"

    def fake_request(method, path, payload=None, **kwargs):
        assert method == "GET"
        assert path == "/v1/projects/project-1/nodes/node-123?view=edit"
        return edit_payload

    mod.request_json = fake_request
    result = mod.node_get({"node_id": "node-123", "view": "edit"})
    assert result["revision"] == "2026-05-06T12:00:00Z"
    assert result["node"]["id"] == "node-123"


def test_node_get_rejects_path_reads(mod):
    try:
        mod.node_get({"path": "/documentation/product/prd", "view": "render"})
    except RuntimeError as exc:
        assert "path is an index/route only" in str(exc)
    else:
        raise AssertionError("path reads should be rejected")


def test_tools_include_node_by_path_descriptor(mod):
    get_tool = next(item for item in mod.tools() if item["name"] == "nap_node_get")
    assert get_tool["inputSchema"].get("required") is None
    assert "node_id" in get_tool["inputSchema"]["properties"]
    assert "uri" in get_tool["inputSchema"]["properties"]
    assert "path" not in get_tool["inputSchema"]["properties"]

    by_id = next(item for item in mod.tools() if item["name"] == "nap_node_by_id")
    assert by_id["inputSchema"]["required"] == ["node_id"]
    assert "nap:// URI" in by_id["description"]

    by_uri = next(item for item in mod.tools() if item["name"] == "nap_node_by_uri")
    assert by_uri["inputSchema"]["required"] == ["uri"]
    assert "nap://" in by_uri["description"]

    tool = next(item for item in mod.tools() if item["name"] == "nap_node_by_path")
    assert tool["inputSchema"]["required"] == ["path"]
    assert "preview_chars" not in tool["inputSchema"]["properties"]
    assert "does not read full node content" in tool["description"]

    assert all(item["name"] != "nap_cat" for item in mod.tools())

    for name in ["nap_discover", "nap_find", "nap_context", "nap_find_related", "nap_recent"]:
        schema = next(item for item in mod.tools() if item["name"] == name)["inputSchema"]
        view = schema["properties"].get("view")
        if view:
            assert "full" not in view["enum"]

    for name in ["nap_patch", "nap_node_patch", "nap_rm"]:
        schema = next(item for item in mod.tools() if item["name"] == name)["inputSchema"]
        assert "node_id" in schema["properties"]
        assert "uri" in schema["properties"]
        assert "path" not in schema["properties"]

    tee_schema = next(item for item in mod.tools() if item["name"] == "nap_tee")["inputSchema"]
    assert tee_schema.get("required") is None
    assert "path" in tee_schema["properties"]
    assert "node_id" in tee_schema["properties"]
    assert "uri" in tee_schema["properties"]

    bulk_node_schema = next(item for item in mod.tools() if item["name"] == "nap_bulk")["inputSchema"]["properties"]["nodes"]["items"]
    assert bulk_node_schema.get("required") is None
    assert "path" in bulk_node_schema["properties"]
    assert "node_id" in bulk_node_schema["properties"]
    assert "uri" in bulk_node_schema["properties"]

    for name in [
        "nap_kanban_start",
        "nap_kanban_send_review",
        "nap_kanban_complete",
        "nap_kanban_block",
        "nap_kanban_unblock",
        "nap_kanban_update",
        "nap_kanban_archive",
    ]:
        schema = next(item for item in mod.tools() if item["name"] == name)["inputSchema"]
        assert "node_id" in schema["properties"]
        assert "uri" in schema["properties"]
        assert "path" not in schema["properties"]

    mv_schema = next(item for item in mod.tools() if item["name"] == "nap_mv")["inputSchema"]
    assert mv_schema["required"] == ["new_path"]
    assert "node_id" in mv_schema["properties"]
    assert "uri" in mv_schema["properties"]
    assert "path" not in mv_schema["properties"]

    ln_schema = next(item for item in mod.tools() if item["name"] == "nap_ln")["inputSchema"]
    assert "source_node_id" in ln_schema["properties"]
    assert "source_uri" in ln_schema["properties"]
    assert "target_node_id" in ln_schema["properties"]
    assert "target_uri" in ln_schema["properties"]
    assert "source_path" not in ln_schema["properties"]
    assert "target_path" not in ln_schema["properties"]

    backlinks_schema = next(item for item in mod.tools() if item["name"] == "nap_backlinks")["inputSchema"]
    assert "node_id" in backlinks_schema["properties"]
    assert "uri" in backlinks_schema["properties"]
    assert "path" not in backlinks_schema["properties"]

    context_schema = next(item for item in mod.tools() if item["name"] == "nap_context")["inputSchema"]
    assert "node_id" in context_schema["properties"]
    assert "node_ids" in context_schema["properties"]
    assert "uri" in context_schema["properties"]
    assert "uris" in context_schema["properties"]
    assert "path" not in context_schema["properties"]
    assert "paths" not in context_schema["properties"]

    related_schema = next(item for item in mod.tools() if item["name"] == "nap_find_related")["inputSchema"]
    assert "node_id" in related_schema["properties"]
    assert "uri" in related_schema["properties"]
    assert "path" not in related_schema["properties"]

    complete_schema = next(item for item in mod.tools() if item["name"] == "nap_plan_complete")["inputSchema"]
    assert "plan_node_id" in complete_schema["properties"]
    assert "plan_uri" in complete_schema["properties"]
    assert "outcome_node_id" in complete_schema["properties"]
    assert "outcome_uri" in complete_schema["properties"]
    assert "path" not in complete_schema["properties"]
    assert "outcome_path" not in complete_schema["properties"]

    supersede_schema = next(item for item in mod.tools() if item["name"] == "nap_plan_supersede")["inputSchema"]
    assert "required" not in supersede_schema
    assert "plan_node_id" in supersede_schema["properties"]
    assert "replacement_node_id" in supersede_schema["properties"]
    assert "path" not in supersede_schema["properties"]
    assert "replacement_path" not in supersede_schema["properties"]

    cancel_schema = next(item for item in mod.tools() if item["name"] == "nap_plan_cancel")["inputSchema"]
    assert "required" not in cancel_schema
    assert "plan_node_id" in cancel_schema["properties"]
    assert "path" not in cancel_schema["properties"]

    plan_to_kanban_schema = next(item for item in mod.tools() if item["name"] == "nap_plan_to_kanban")["inputSchema"]
    assert "plan_node_id" in plan_to_kanban_schema["properties"]
    assert "plan_uri" in plan_to_kanban_schema["properties"]
    assert "plan_path" not in plan_to_kanban_schema["properties"]

    for name in ["nap_agent_cat", "nap_agent_patch", "nap_agent_rm"]:
        schema = next(item for item in mod.tools() if item["name"] == name)["inputSchema"]
        assert schema["required"] == ["agent_slug"]
        assert "node_id" in schema["properties"]
        assert "uri" in schema["properties"]
        assert "path" not in schema["properties"]

    agent_tee_schema = next(item for item in mod.tools() if item["name"] == "nap_agent_tee")["inputSchema"]
    assert agent_tee_schema["required"] == ["agent_slug"]
    assert "path" in agent_tee_schema["properties"]
    assert "node_id" in agent_tee_schema["properties"]
    assert "uri" in agent_tee_schema["properties"]

    agent_ln_schema = next(item for item in mod.tools() if item["name"] == "nap_agent_ln")["inputSchema"]
    assert agent_ln_schema["required"] == ["agent_slug"]
    assert "source_node_id" in agent_ln_schema["properties"]
    assert "target_node_id" in agent_ln_schema["properties"]
    assert "source_path" not in agent_ln_schema["properties"]
    assert "target_path" not in agent_ln_schema["properties"]


def test_mutation_tools_reject_path_and_patch_by_node_id(mod):
    found = node("/notes/current", "active", node_type="note")
    found["id"] = "node-123"
    writes = []

    mod.get_node_by_id = lambda args: found
    mod.request_project_write = lambda method, path, payload, *args, **kwargs: writes.append((method, path, payload, args)) or {
        **found,
        "content_text": payload.get("content_text", found["content_text"]) if payload else found["content_text"],
    }
    mod.index_node = lambda node: None
    mod.remove_indexed_node = lambda node_id: None

    updated = mod.update_node_by_path({"node_id": "node-123", "content_text": "updated"})
    assert updated["updated"] is True
    assert writes[-1][0] == "PATCH"
    assert writes[-1][1] == "/v1/projects/project-1/nodes/node-123"
    assert writes[-1][3][2:] == ("node", "node-123")

    patched = mod.guarded_node_patch({
        "node_id": "node-123",
        "precondition": {"revision": "r1", "read_fingerprint": "fp1"},
        "set": {"metadata": {"status": "active"}},
    })
    assert patched["id"] == "node-123"
    assert writes[-1][1] == "/v1/projects/project-1/nodes/node-123/patch"

    archived = mod.archive_node_by_path({"node_id": "node-123"})
    assert archived["archived"] is True
    assert writes[-1][0] == "DELETE"
    assert writes[-1][1] == "/v1/projects/project-1/nodes/node-123"

    with pytest.raises(RuntimeError, match="path is an index/route only"):
        mod.update_node_by_path({"path": "/notes/current", "content_text": "bad"})


def test_graph_tools_use_node_identity_instead_of_paths(mod):
    source = node("/notes/source", "active", node_type="note")
    source["id"] = "source-123"
    target = node("/notes/target", "active", node_type="note")
    target["id"] = "target-123"
    writes = []
    reads = []

    def fake_get_node_by_id(args):
        reads.append(args)
        node_id = args.get("node_id")
        if node_id == "source-123":
            return source
        if node_id == "target-123":
            return target
        raise AssertionError(f"unexpected node_id {node_id}")

    def fake_write(method, path, payload, *args, **kwargs):
        writes.append((method, path, payload, args))
        return {**source, "full_path": "/notes/source-renamed" if payload.get("name") == "source-renamed" else source["full_path"], "links": payload.get("links", source["links"])}

    def fake_request(method, path, payload=None, **kwargs):
        assert method == "GET"
        assert path == "/v1/projects/project-1/nodes/target-123/backlinks"
        return {"items": [source]}

    mod.get_node_by_id = fake_get_node_by_id
    mod.request_project_write = fake_write
    mod.request_json = fake_request
    mod.index_node = lambda node: None
    mod.inbound_reference_plan = lambda path: [{"id": "other-1", "path": "/notes/other"}]
    mod.remove_indexed_node = lambda node_id: None

    linked = mod.link_nodes({"source_node_id": "source-123", "target_node_id": "target-123", "relation": "references"})
    assert linked["created"] is True
    assert writes[-1][0] == "PATCH"
    assert writes[-1][1] == "/v1/projects/project-1/nodes/source-123"
    assert writes[-1][2]["links"] == [{"path": "/notes/target", "relation": "references"}]
    assert writes[-1][3][2:] == ("node", "source-123")

    backlinks = mod.get_backlinks_by_path({"node_id": "target-123"})
    assert backlinks["target"]["id"] == "target-123"

    moved = mod.move_node({"node_id": "source-123", "new_path": "/notes/source-renamed"})
    assert moved["moved"] is True
    assert writes[-1][1] == "/v1/projects/project-1/nodes/source-123"
    assert writes[-1][2]["folder_path"] == "/notes"
    assert writes[-1][2]["name"] == "source-renamed"
    assert writes[-1][3][2:] == ("node", "source-123")

    with pytest.raises(RuntimeError, match="paths are index/routes only"):
        mod.link_nodes({"source_path": "/notes/source", "target_path": "/notes/target"})
    with pytest.raises(RuntimeError, match="path is an index/route only"):
        mod.get_backlinks_by_path({"path": "/notes/target"})
    with pytest.raises(RuntimeError, match="path is an index/route only"):
        mod.move_node({"path": "/notes/source", "new_path": "/notes/source-renamed"})


def test_tee_and_bulk_create_by_path_update_by_identity(mod):
    existing = node("/notes/current", "active", node_type="note")
    existing["id"] = "node-123"
    writes = []

    mod.try_get_node_by_path = lambda path, allow_agent=False: existing if path == "/notes/current" else None
    mod.get_node_by_id = lambda args: existing
    mod.request_project_write = lambda method, path, payload, *args, **kwargs: writes.append((method, path, payload, args)) or {
        **existing,
        "id": "node-123" if method == "PATCH" else "node-new",
        "full_path": "/notes/new" if method == "POST" else existing["full_path"],
        "content_text": payload.get("content_text", existing["content_text"]),
    }
    mod.index_node = lambda node: None
    mod.acquire_project_lock = lambda args: {"id": "lock-1", "lease_token": "lease-1"}
    mod.lock_headers = lambda lock: {"X-Lock": lock["id"]}
    mod.release_project_lock = lambda args: None
    mod.request_json = lambda method, path, payload=None, **kwargs: writes.append((method, path, payload, ())) or {
        **existing,
        "content_text": payload.get("content_text", existing["content_text"]) if payload else existing["content_text"],
    }

    created = mod.upsert_node({"path": "/notes/new", "content_text": "new"})
    assert created["created"] is True
    assert writes[-1][0] == "POST"

    updated = mod.upsert_node({"node_id": "node-123", "content_text": "updated"})
    assert updated["updated"] is True
    assert writes[-1][0] == "PATCH"
    assert writes[-1][1] == "/v1/projects/project-1/nodes/node-123"

    with pytest.raises(RuntimeError, match="path already resolves"):
        mod.upsert_node({"path": "/notes/current", "content_text": "bad"})

    bulk = mod.bulk_upsert_nodes({"nodes": [{"node_id": "node-123", "content_text": "bulk update"}]})
    assert bulk["items"][0]["created"] is False
    assert writes[-1][1] == "/v1/projects/project-1/nodes/node-123"

    with pytest.raises(RuntimeError, match="path already resolves"):
        mod.bulk_upsert_nodes({"nodes": [{"path": "/notes/current", "content_text": "bad"}]})


def test_agent_tools_create_by_route_and_update_by_identity(mod):
    agent_file = node("/agents/alice/memory/current", "active", node_type="agent-file")
    agent_file["id"] = "agent-node-123"
    target = node("/notes/target", "active", node_type="note")
    target["id"] = "target-123"
    writes = []

    def fake_get_node_by_id(args):
        node_id = args.get("node_id")
        if node_id == "agent-node-123":
            return agent_file
        if node_id == "target-123":
            return target
        raise AssertionError(f"unexpected node_id {node_id}")

    mod.get_node_by_id = fake_get_node_by_id
    mod.try_get_node_by_path = lambda path, allow_agent=False: agent_file if path == "/agents/alice/memory/current" else None
    mod.request_project_write = lambda method, path, payload, *args, **kwargs: writes.append((method, path, payload, args)) or {
        **agent_file,
        "id": "agent-node-new" if method == "POST" else agent_file["id"],
        "full_path": f"{payload.get('folder_path')}/{payload.get('name')}" if method == "POST" else agent_file["full_path"],
        "content_text": payload.get("content_text", agent_file["content_text"]) if payload else agent_file["content_text"],
        "links": payload.get("links", agent_file["links"]) if payload else agent_file["links"],
    }
    mod.index_node = lambda node: None
    mod.remove_indexed_node = lambda node_id: None

    read = mod.get_agent_node({"agent_slug": "alice", "node_id": "agent-node-123"})
    assert read["id"] == "agent-node-123"

    created = mod.upsert_agent_node({"agent_slug": "alice", "path": "/memory/new", "content_text": "new"})
    assert created["created"] is True
    assert writes[-1][0] == "POST"
    assert writes[-1][2]["folder_path"] == "/agents/alice/memory"

    updated = mod.upsert_agent_node({"agent_slug": "alice", "node_id": "agent-node-123", "content_text": "updated"})
    assert updated["updated"] is True
    assert writes[-1][1] == "/v1/projects/project-1/nodes/agent-node-123"
    assert writes[-1][3][2:] == ("node", "agent-node-123")

    patched = mod.update_agent_node({"agent_slug": "alice", "node_id": "agent-node-123", "content_text": "patched"})
    assert patched["updated"] is True

    archived = mod.archive_agent_node({"agent_slug": "alice", "node_id": "agent-node-123"})
    assert archived["archived"] is True
    assert writes[-1][0] == "DELETE"
    assert writes[-1][3][2:] == ("node", "agent-node-123")

    linked = mod.link_agent_node({"agent_slug": "alice", "source_node_id": "agent-node-123", "target_node_id": "target-123"})
    assert linked["created"] is True
    assert writes[-1][2]["links"] == [{"path": "/notes/target", "relation": "references"}]
    assert writes[-1][3][2:] == ("node", "agent-node-123")

    with pytest.raises(RuntimeError, match="path already resolves"):
        mod.upsert_agent_node({"agent_slug": "alice", "path": "/memory/current", "content_text": "bad"})
    with pytest.raises(RuntimeError, match="path is an index/route only"):
        mod.update_agent_node({"agent_slug": "alice", "path": "/memory/current", "content_text": "bad"})
    with pytest.raises(RuntimeError, match="paths are index/routes only"):
        mod.link_agent_node({"agent_slug": "alice", "source_path": "/memory/current", "target_path": "/notes/target"})


def test_context_and_related_use_identity_inputs(mod):
    root = node("/notes/root", "active", node_type="note", links=[{"path": "/notes/linked", "relation": "references"}], tags=["topic"])
    root["id"] = "root-123"
    linked = node("/notes/linked", "active", node_type="note", tags=["topic"])
    linked["id"] = "linked-123"
    backlink = node("/notes/backlink", "active", node_type="note")
    backlink["id"] = "backlink-123"
    reads = []

    def fake_get_node_by_id(args):
        reads.append(args)
        node_id = args.get("node_id")
        if node_id == "root-123":
            return root
        if node_id == "linked-123":
            return linked
        raise AssertionError(f"unexpected node_id {node_id}")

    def fake_read_by_path(args, allow_agent=False):
        if args["path"] == "/notes/linked":
            return linked
        if args["path"] == "/notes/backlink":
            return backlink
        raise RuntimeError("HTTP 404")

    def fake_request(method, path, payload=None, **kwargs):
        assert method == "GET"
        if path == "/v1/projects/project-1/nodes/root-123/backlinks":
            return {"items": [backlink]}
        if path == "/v1/projects/project-1/nodes/linked-123/backlinks":
            return {"items": []}
        raise AssertionError(path)

    mod.get_node_by_id = fake_get_node_by_id
    mod.read_node_by_path = fake_read_by_path
    mod.request_json = fake_request
    mod.list_project_nodes = lambda args: {"items": [root, linked]}

    context = mod.get_memory_context({"node_id": "root-123", "depth": 1, "view": "paths"})
    assert [item["full_path"] for item in context["items"]] == ["/notes/root", "/notes/linked", "/notes/backlink"]
    assert {"source": "/notes/root", "target": "/notes/linked", "relation": "references"} in context["edges"]
    assert {"source": "/notes/backlink", "target": "/notes/root", "relation": "backlink"} in context["edges"]

    related = mod.find_related_nodes({"node_id": "root-123", "view": "paths"})
    assert related["target"]["full_path"] == "/notes/root"
    assert {item["full_path"] for item in related["items"]} >= {"/notes/backlink", "/notes/linked"}

    with pytest.raises(RuntimeError, match="paths are index/routes only"):
        mod.get_memory_context({"path": "/notes/root"})
    with pytest.raises(RuntimeError, match="path is an index/route only"):
        mod.find_related_nodes({"path": "/notes/root"})


def test_discovery_rejects_full_content_view(mod):
    with pytest.raises(RuntimeError, match="full node bodies require nap_node_get"):
        mod.list_project_nodes({"view": "full"})


def test_include_content_is_legacy_noop(mod):
    found = node("/plans/found", "planned", content="body must not be returned")

    def fake_request(method, path, payload=None, **kwargs):
        assert method == "GET"
        assert "view=paths" in path
        return {"items": [found], "next_cursor": None}

    mod.request_json = fake_request
    result = mod.list_project_nodes({"include_content": True, "view": "paths"})
    assert result["view"] == "paths"
    assert "content_text" not in result["items"][0]


def test_recent_memory_defaults_to_paths(mod):
    recent = node(
        "/notes/recent-title",
        "active",
        metadata={"title": "Readable Recent Title", "priority": "high"},
        tags=["recent"],
        content="large body that should not appear in the default recent response",
        node_type="note",
    )

    def fake_request(method, path, payload=None, **kwargs):
        assert method == "GET"
        assert "/nodes?" in path
        assert "view=paths" in path
        return {"items": [recent], "next_cursor": None}

    mod.request_json = fake_request
    result = mod.recent_memory({})
    assert result["ok"] is True
    assert result["view"] == "paths"
    assert result["items"] == [{
        "full_path": "/notes/recent-title",
        "type": "note",
        "status": "active",
        "updated_at": "2026-05-06T12:00:00Z",
    }]
    assert "content_text" not in result["items"][0]
    assert "metadata_summary" not in result["items"][0]


def test_recent_memory_accepts_date_aliases_and_returns_dates(mod):
    recent = node(
        "/notes/recent-title",
        "active",
        metadata={"date": "2026-05-06"},
        tags=["recent"],
        node_type="note",
    )
    recent["created_at"] = "2026-05-05T10:00:00Z"

    def fake_request(method, path, payload=None, **kwargs):
        assert method == "GET"
        assert "updated_after=2026-05-05T00%3A00%3A00Z" in path
        assert "updated_before=2026-05-06T23%3A59%3A59Z" in path
        return {"items": [recent], "next_cursor": None}

    mod.request_json = fake_request
    result = mod.recent_memory({"since": "2026-05-05", "until": "2026-05-06"})
    assert result["items"] == [{
        "full_path": "/notes/recent-title",
        "type": "note",
        "status": "active",
        "date": "2026-05-06",
        "created_at": "2026-05-05T10:00:00Z",
        "updated_at": "2026-05-06T12:00:00Z",
    }]


def test_find_defaults_to_paths_and_requests_compact_rest_view(mod):
    found = node(
        "/plans/found",
        "planned",
        content="large body that should not be fetched by default",
    )

    def fake_request(method, path, payload=None, **kwargs):
        assert method == "GET"
        assert "/nodes?" in path
        assert "folder_path=%2Fplans" in path
        assert "view=paths" in path
        return {"items": [found], "next_cursor": None}

    mod.request_json = fake_request
    result = mod.list_project_nodes({"folder_path": "/plans"})
    assert result["ok"] is True
    assert result["view"] == "paths"
    assert result["items"] == [{
        "full_path": "/plans/found",
        "type": "plan",
        "status": "planned",
        "updated_at": "2026-05-06T12:00:00Z",
    }]
    assert "content_text" not in result["items"][0]


def test_discover_uses_search_when_keywords_present(mod):
    calls = []

    def fake_search(args):
        calls.append(args)
        return {"ok": True, "items": [], "view": args.get("view")}

    mod.search_memory = fake_search
    result = mod.discover_memory({"keywords": "gateway relay", "since": "2026-05-01"})
    assert result["tool_intent"] == "search"
    assert result["range"]["updated_after"] == "2026-05-01T00:00:00Z"
    assert calls and calls[-1]["q"] == "gateway relay"
    assert calls[-1]["view"] == "summary"
    assert "date" in result["awareness"]["date_fields_in_rows"]


def test_classify_query_points_exact_paths_to_context(mod):
    result = mod.classify_memory_query({"q": "explain /documentation/security/overview since May", "since": "2026-05-01"})
    assert result["classification"]["primary_tool"] == "nap_context"
    assert result["classification"]["exact_paths"] == ["/documentation/security/overview"]
    assert result["classification"]["range"]["updated_after"] == "2026-05-01T00:00:00Z"


def test_title_view_requests_summary_rest_view_without_content(mod):
    found = node(
        "/notes/titled",
        "active",
        metadata={"title": "Readable Title"},
        content="large body that should not be fetched by title view",
    )

    def fake_request(method, path, payload=None, **kwargs):
        assert method == "GET"
        assert "/nodes?" in path
        assert "view=summary" in path
        return {"items": [found], "next_cursor": None}

    mod.request_json = fake_request
    result = mod.list_project_nodes({"view": "titles"})
    assert result["ok"] is True
    assert result["view"] == "titles"
    assert result["items"] == [{"title": "Readable Title", "full_path": "/notes/titled"}]
    assert "content_text" not in result["items"][0]


def test_rest_query_string_serializes_booleans_lowercase(mod):
    query = mod.rest_query_string({
        "folder_path": "/kanban",
        "include_archived": False,
        "archived_only": False,
        "active_only": True,
        "status": ["todo", "doing"],
    })

    assert "include_archived=false" in query
    assert "archived_only=false" in query
    assert "active_only=true" in query
    assert "include_archived=False" not in query
    assert "active_only=True" not in query
    assert "status=todo" in query
    assert "status=doing" in query


def test_kanban_list_grouping_and_filters(mod):
    doing = node(
        "/kanban/doing/fix-wrapper",
        "doing",
        metadata={"column": "doing", "priority": "high", "owner": "codex", "blocked": True},
        tags=["kanban"],
        node_type="kanban_card",
    )
    todo = node(
        "/kanban/todo/add-tests",
        "todo",
        metadata={"column": "todo", "priority": "normal", "owner": "codex", "blocked": False},
        tags=["kanban"],
        node_type="kanban_card",
    )
    done = node(
        "/kanban/done/old-card",
        "done",
        metadata={"column": "done", "owner": "codex"},
        tags=["kanban"],
        node_type="kanban_card",
    )
    outside = node("/plans/not-kanban", "planned")

    def fake_request(method, path, payload=None, **kwargs):
        assert method == "GET"
        assert "q=" in path
        assert "include_archived=false" in path
        assert "archived_only=false" in path
        return {"items": [doing, todo, done, outside], "next_cursor": None}

    mod.request_json = fake_request
    result = mod.list_kanban_cards({"owner": "codex", "blocked": True, "group_by": "column"})
    assert result["ok"] is True
    assert result["kanban_root"] == "/kanban"
    assert [item["full_path"] for item in result["items"]] == ["/kanban/doing/fix-wrapper"]
    assert "content_text" not in result["items"][0]
    assert result["items"][0]["column"] == "doing"
    assert result["items"][0]["blocked"] is True
    assert list(result["groups"]) == ["doing"]


def test_kanban_dependency_summary_filters_ready_and_blocking(mod):
    plan = node(
        "/kanban/backlog/plan",
        "backlog",
        metadata={"column": "backlog", "priority": "urgent", "blocked": False},
        tags=["kanban"],
        node_type="kanban_card",
        links=[{"path": "/kanban/backlog/implementation", "relation": "blocks"}],
    )
    implementation = node(
        "/kanban/todo/implementation",
        "todo",
        metadata={
            "column": "todo",
            "priority": "high",
            "blocked_by": "/kanban/backlog/plan",
            "external_blockers": [{"id": "deploy", "state": "open", "strength": "hard"}],
        },
        tags=["kanban"],
        node_type="kanban_card",
        links=[
            {"path": "/kanban/backlog/plan", "relation": "blocked-by"},
            {"path": "/kanban/backlog/context", "relation": "relates-to"},
            {"path": "/kanban/backlog/child", "relation": "parent_of"},
            {"path": "/kanban/backlog/canonical", "relation": "duplicate-of"},
        ],
    )

    def fake_request(method, path, payload=None, **kwargs):
        assert method == "GET"
        return {"items": [plan, implementation], "next_cursor": None}

    mod.request_json = fake_request

    blocked = mod.list_kanban_cards({"dependency_view": "blocked", "limit": 10})
    assert [item["full_path"] for item in blocked["items"]] == ["/kanban/todo/implementation"]
    assert blocked["items"][0]["dependency_summary"]["hard_blockers"] == 1
    assert blocked["items"][0]["dependency_summary"]["external_blockers"] == 1
    assert blocked["items"][0]["hard_blockers"] == ["/kanban/backlog/plan"]
    assert blocked["items"][0]["related"] == ["/kanban/backlog/context"]
    assert blocked["items"][0]["children"] == ["/kanban/backlog/child"]
    assert blocked["items"][0]["duplicates"] == ["/kanban/backlog/canonical"]

    ready = mod.list_kanban_cards({"dependency_view": "ready", "limit": 10})
    assert [item["full_path"] for item in ready["items"]] == ["/kanban/backlog/plan"]

    blocking = mod.list_kanban_cards({"dependency_view": "blocking", "limit": 10})
    assert blocking["items"][0]["blocking"] == ["/kanban/backlog/implementation"]


def test_kanban_list_reads_beyond_first_source_page(mod):
    first_page = [
        node(
            "/kanban/todo/other-card",
            "todo",
            metadata={"column": "todo", "owner": "codex", "blocked": False},
            tags=["kanban"],
            node_type="kanban_card",
        )
    ]
    second_page = [
        node(
            "/kanban/doing/blocked-card",
            "doing",
            metadata={"column": "doing", "owner": "codex", "blocked": True},
            tags=["kanban"],
            node_type="kanban_card",
        )
    ]
    calls = []

    def fake_request(method, path, payload=None, **kwargs):
        calls.append(path)
        assert method == "GET"
        assert "q=%2Fkanban" in path
        assert "limit=10000" in path
        assert "include_archived=false" in path
        assert "archived_only=false" in path
        if "cursor=page-2" in path:
            return {"items": second_page, "next_cursor": None}
        return {"items": first_page, "next_cursor": "page-2"}

    mod.request_json = fake_request
    result = mod.list_kanban_cards({"blocked": True, "limit": 10})
    assert [item["full_path"] for item in result["items"]] == ["/kanban/doing/blocked-card"]
    assert result["truncated"] is False
    assert len(calls) == 2


def test_kanban_workflow_reads_pending_and_pick_next(mod):
    backlog_urgent = node(
        "/kanban/backlog/later-urgent",
        "backlog",
        metadata={"column": "backlog", "priority": "urgent", "rank": "00001024", "blocked": False},
        tags=["kanban"],
        node_type="kanban_card",
    )
    todo_high = node(
        "/kanban/todo/next-high",
        "todo",
        metadata={"column": "todo", "priority": "high", "rank": "00002048", "blocked": False},
        tags=["kanban"],
        node_type="kanban_card",
    )
    todo_blocked = node(
        "/kanban/todo/blocked",
        "todo",
        metadata={"column": "todo", "priority": "urgent", "rank": "00001024", "blocked": True},
        tags=["kanban"],
        node_type="kanban_card",
    )

    def fake_request(method, path, payload=None, **kwargs):
        assert method == "GET"
        return {"items": [backlog_urgent, todo_high, todo_blocked], "next_cursor": None}

    mod.request_json = fake_request
    pending = mod.get_pending_kanban_cards({})
    assert [item["full_path"] for item in pending["items"]] == ["/kanban/todo/next-high", "/kanban/backlog/later-urgent"]

    picked = mod.pick_next_kanban_card({})
    assert picked["selected"]["full_path"] == "/kanban/todo/next-high"
    assert picked["selection"]["blocked_excluded"] is True


def test_kanban_workflow_create_move_and_block(mod):
    writes = []
    existing = node(
        "/kanban/todo/add-mcp-commands",
        "todo",
        metadata={"column": "todo", "priority": "normal", "rank": "00001024", "blocked": False, "title": "Add MCP commands"},
        tags=["kanban"],
        node_type="kanban_card",
    )

    mod.list_kanban_cards = lambda args: {"items": []}
    mod.request_project_write = lambda method, path, payload, *args, **kwargs: writes.append((method, path, payload)) or {
        **existing,
        "id": "id-add-mcp-commands",
        "full_path": f"{payload.get('folder_path', '/kanban/todo')}/{payload.get('name', 'add-mcp-commands')}".replace("//", "/"),
        "folder_path": payload.get("folder_path", "/kanban/todo"),
        "name": payload.get("name", "add-mcp-commands"),
        "metadata": payload.get("metadata", existing["metadata"]),
        "content_text": payload.get("content_text", existing["content_text"]),
        "tags": payload.get("tags", existing["tags"]),
    }
    mod.get_node_by_id = lambda args: {**existing, "id": "id-add-mcp-commands"}
    mod.index_node = lambda node: None
    mod.remove_indexed_node = lambda node_id: None

    created = mod.create_kanban_card({"title": "Add MCP commands", "priority": "HIGH"})
    assert created["created"] is True
    assert "node" not in created
    assert writes[0][0] == "POST"
    assert writes[0][2]["folder_path"] == "/kanban/todo"
    assert writes[0][2]["metadata"]["priority"] == "high"
    assert writes[0][2]["metadata"]["rank"] == "00001024"

    moved = mod.move_kanban_card({"node_id": "id-add-mcp-commands"}, "doing")
    assert moved["to"] == "/kanban/doing/add-mcp-commands"
    assert "node" not in moved
    assert writes[1][0] == "PATCH"
    assert writes[1][2]["folder_path"] == "/kanban/doing"
    assert writes[1][2]["metadata"]["column"] == "doing"
    assert writes[1][2]["metadata"]["lifecycle_state"] == "active"

    blocked = mod.block_kanban_card({"node_id": "id-add-mcp-commands", "blocked_reason": "waiting"}, True)
    assert blocked["updated"] is True
    assert "node" not in blocked
    assert writes[2][2]["metadata"]["blocked"] is True
    assert writes[2][2]["metadata"]["blocked_by"] == "waiting"

    with pytest.raises(RuntimeError, match="path is an index/route only"):
        mod.move_kanban_card({"path": "/kanban/todo/add-mcp-commands"}, "doing")


def test_kanban_create_deduplicates_overlapping_titles(mod):
    writes = []
    existing_cards = [
        node("/kanban/todo/add-mcp-commands", "todo", metadata={"column": "todo"}, tags=["kanban"], node_type="kanban_card"),
        node("/kanban/todo/add-mcp-commands-20260510123456", "todo", metadata={"column": "todo"}, tags=["kanban"], node_type="kanban_card"),
        node("/kanban/doing/add-mcp-commands", "doing", metadata={"column": "doing"}, tags=["kanban"], node_type="kanban_card"),
    ]

    mod.list_kanban_cards = lambda args: {"items": existing_cards if args.get("column") == "todo" else []}
    mod.request_project_write = lambda method, path, payload, *args, **kwargs: writes.append((method, path, payload)) or {
        **existing_cards[0],
        "id": "id-deduped",
        "full_path": f"{payload.get('folder_path')}/{payload.get('name')}",
        "folder_path": payload.get("folder_path"),
        "name": payload.get("name"),
        "metadata": payload.get("metadata", {}),
        "tags": payload.get("tags", []),
    }
    mod.index_node = lambda node: None

    created = mod.create_kanban_card({"title": "Add MCP commands", "column": "todo", "now": "2026-05-10T12:34:56Z"})
    assert created["path"] == "/kanban/todo/add-mcp-commands-20260510123456-2"
    assert "node" not in created
    assert writes[0][2]["name"] == "add-mcp-commands-20260510123456-2"


def test_lineage_status_reports_generic_plan_and_card_coverage(mod):
    linked_plan = node("/plans/linked", "planned", links=[{"path": "/decisions/source", "relation": "implements"}])
    unlinked_plan = node("/plans/unlinked", "planned")
    linked_card = node(
        "/kanban/todo/linked",
        "todo",
        metadata={"column": "todo"},
        tags=["kanban"],
        node_type="kanban_card",
        links=[{"path": "/plans/linked", "relation": "derived-from"}],
    )
    unlinked_card = node(
        "/kanban/todo/unlinked",
        "todo",
        metadata={"column": "todo"},
        tags=["kanban"],
        node_type="kanban_card",
    )

    def fake_list(args):
        if args.get("folder_path") == "/plans":
            return {"items": [linked_plan, unlinked_plan]}
        if args.get("q") == "/kanban":
            return {"items": [linked_card, unlinked_card, node("/notes/not-kanban", "active")]}
        return {"items": []}

    mod.list_project_nodes = fake_list
    result = mod.lineage_status({})
    assert result["status"] == "warnings"
    assert result["counts"]["plans"] == 2
    assert result["counts"]["plans_with_source_record"] == 1
    assert result["counts"]["kanban_cards"] == 2
    assert result["counts"]["kanban_cards_with_origin_plan"] == 1
    assert [warning["type"] for warning in result["warnings"]] == [
        "plan_missing_source_record",
        "kanban_card_missing_origin_plan",
    ]


def test_plan_to_kanban_creates_card_with_origin_plan_link(mod):
    writes = []
    plan = node(
        "/plans/generic-work",
        "planned",
        metadata={"title": "Generic Work", "priority": "high", "owner": "codex"},
        tags=["plan"],
        content="Implement a generic record to plan to kanban flow.",
    )

    mod.get_node_by_id = lambda args: plan
    mod.list_kanban_cards = lambda args: {"items": []}
    mod.request_project_write = lambda method, path, payload, *args, **kwargs: writes.append((method, path, payload)) or {
        "id": "id-generic-work",
        "project_id": "project-1",
        "full_path": f"{payload.get('folder_path')}/{payload.get('name')}",
        "folder_path": payload.get("folder_path"),
        "name": payload.get("name"),
        "type": payload.get("type"),
        "metadata": payload.get("metadata", {}),
        "tags": payload.get("tags", []),
        "links": payload.get("links", []),
        "content_text": payload.get("content_text", ""),
        "updated_at": "2026-05-06T12:00:00Z",
    }
    mod.index_node = lambda node: None

    created = mod.plan_to_kanban_card({"plan_node_id": plan["id"], "column": "todo"})
    assert created["path"] == "/kanban/todo/generic-work"
    assert created["source_plan_path"] == "/plans/generic-work"
    assert writes[0][0] == "POST"
    assert writes[0][2]["links"] == [{"path": "/plans/generic-work", "relation": "derived-from"}]
    assert writes[0][2]["metadata"]["source_plan_path"] == "/plans/generic-work"
    assert writes[0][2]["metadata"]["priority"] == "high"
    assert writes[0][2]["metadata"]["owner"] == "codex"


def test_plan_lifecycle_dry_run_and_write(mod):
    plan = node("/plans/example", "in_progress", tags=["plan"])
    plan["id"] = "plan-123"
    outcome = node("/implementation-notes/example", "completed", node_type="implementation-note")
    outcome["id"] = "outcome-123"
    writes = []

    def fake_get_by_id(args):
        if args["node_id"] == plan["id"]:
            return plan
        if args["node_id"] == outcome["id"]:
            return outcome
        raise AssertionError(f"unexpected node id {args['node_id']}")

    mod.get_node_by_id = fake_get_by_id
    mod.update_node_by_path = lambda args: writes.append(("update", args)) or {"node": {**plan, **args}}
    mod.archive_node_by_path = lambda args: writes.append(("archive", args)) or {"archived": True}

    dry = mod.complete_plan({"plan_node_id": plan["id"], "outcome_node_id": outcome["id"], "dry_run": True})
    assert dry["changed"] is True
    assert dry["dry_run"] is True
    assert dry["status"] == "completed"
    assert "archive node" in dry["planned_changes"]
    assert writes == []

    written = mod.complete_plan({"plan_node_id": plan["id"], "outcome_node_id": outcome["id"], "reason": "implemented"})
    assert written["changed"] is True
    assert written["archived"] is True
    assert writes[0][0] == "update"
    assert writes[0][1]["metadata"]["status"] == "completed"
    assert writes[0][1]["metadata"]["outcome_path"] == "/implementation-notes/example"
    assert "completed" in writes[0][1]["tags"]
    assert "archived" in writes[0][1]["tags"]
    assert writes[0][1]["links"] == [{"path": "/implementation-notes/example", "relation": "implemented-by"}]
    assert writes[1][0] == "archive"
    assert writes[1][1]["node_id"] == plan["id"]

    with pytest.raises(RuntimeError, match="path is an index/route only"):
        mod.complete_plan({"path": "/plans/example", "outcome_path": "/implementation-notes/example"})


def test_plan_lifecycle_archived_idempotent(mod):
    outcome = node("/implementation-notes/example", "completed", node_type="implementation-note")
    outcome["id"] = "outcome-123"
    archived = node(
        "/plans/example",
        "completed",
        metadata={"outcome_path": "/implementation-notes/example"},
        tags=["plan", "completed", "archived"],
        archived_at="2026-05-06T12:01:00Z",
    )
    archived["id"] = "plan-123"
    archived["links"] = [{"path": "/implementation-notes/example", "relation": "implemented-by"}]

    def fake_get_by_id(args):
        if args["node_id"] == outcome["id"]:
            return outcome
        raise RuntimeError("HTTP 404")

    mod.get_node_by_id = fake_get_by_id
    mod.list_project_nodes = lambda args: {"items": [archived]}
    result = mod.complete_plan({"plan_node_id": archived["id"], "outcome_node_id": outcome["id"]})
    assert result["ok"] is True
    assert result["changed"] is False
    assert result["archived"] is True


def test_tool_registry(mod):
    names = {tool["name"] for tool in mod.raw_tools()}
    for name in [
        "nap_plan_list_active",
        "nap_lineage_status",
        "nap_kanban_list",
        "nap_kanban_get_pending",
        "nap_kanban_pick_next",
        "nap_kanban_start",
        "nap_kanban_complete",
        "nap_plan_complete",
        "nap_plan_supersede",
        "nap_plan_cancel",
        "nap_plan_to_kanban",
    ]:
        assert name in names
    assert mod.tool_contract_metadata("nap_plan_complete")["dry_run_supported"] is True
    assert mod.tool_contract_metadata("nap_plan_list_active")["side_effect"] == "none"
    assert mod.tool_contract_metadata("nap_lineage_status")["side_effect"] == "none"
    assert mod.tool_contract_metadata("nap_kanban_get_pending")["side_effect"] == "none"
    assert mod.tool_contract_metadata("nap_plan_to_kanban")["side_effect"] == "remote-write"
    assert mod.tool_contract_metadata("nap_kanban_start")["side_effect"] == "remote-write"


def run():
    mod = load_module()
    test_active_plan_listing(mod)
    mod = load_module()
    test_node_discovery_allows_large_requested_limit(mod)
    mod = load_module()
    test_node_by_path_returns_canonical_identity_uri(mod)
    mod = load_module()
    test_node_by_id_returns_canonical_identity_uri(mod)
    mod = load_module()
    test_node_by_uri_resolves_canonical_uri(mod)
    mod = load_module()
    test_node_get_accepts_node_id_without_path(mod)
    mod = load_module()
    test_node_get_rejects_path_reads(mod)
    mod = load_module()
    test_tools_include_node_by_path_descriptor(mod)
    mod = load_module()
    test_recent_memory_defaults_to_paths(mod)
    mod = load_module()
    test_find_defaults_to_paths_and_requests_compact_rest_view(mod)
    mod = load_module()
    test_title_view_requests_summary_rest_view_without_content(mod)
    mod = load_module()
    test_kanban_list_grouping_and_filters(mod)
    mod = load_module()
    test_kanban_dependency_summary_filters_ready_and_blocking(mod)
    mod = load_module()
    test_kanban_list_reads_beyond_first_source_page(mod)
    mod = load_module()
    test_kanban_workflow_reads_pending_and_pick_next(mod)
    mod = load_module()
    test_kanban_workflow_create_move_and_block(mod)
    mod = load_module()
    test_kanban_create_deduplicates_overlapping_titles(mod)
    mod = load_module()
    test_lineage_status_reports_generic_plan_and_card_coverage(mod)
    mod = load_module()
    test_plan_to_kanban_creates_card_with_origin_plan_link(mod)
    mod = load_module()
    test_plan_lifecycle_dry_run_and_write(mod)
    mod = load_module()
    test_plan_lifecycle_archived_idempotent(mod)
    mod = load_module()
    test_tool_registry(mod)
    print("ok: MCP memory ergonomics helpers smoke passed")


if __name__ == "__main__":
    run()
