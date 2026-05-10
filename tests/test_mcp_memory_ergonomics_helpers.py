#!/usr/bin/env python3
"""Smoke-test active plan, kanban, and plan lifecycle MCP helpers."""

import importlib.util
import pathlib
import sys


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


def node(path, status="planned", metadata=None, tags=None, content="body text", archived_at=None, node_type="plan"):
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
        "links": [],
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
    mod.get_node_by_path = lambda args, allow_agent=False: existing
    mod.index_node = lambda node: None
    mod.remove_indexed_node = lambda node_id: None

    created = mod.create_kanban_card({"title": "Add MCP commands", "priority": "HIGH"})
    assert created["created"] is True
    assert writes[0][0] == "POST"
    assert writes[0][2]["folder_path"] == "/kanban/todo"
    assert writes[0][2]["metadata"]["priority"] == "high"
    assert writes[0][2]["metadata"]["rank"] == "00001024"

    moved = mod.move_kanban_card({"path": "/kanban/todo/add-mcp-commands"}, "doing")
    assert moved["to"] == "/kanban/doing/add-mcp-commands"
    assert writes[1][0] == "PATCH"
    assert writes[1][2]["folder_path"] == "/kanban/doing"
    assert writes[1][2]["metadata"]["column"] == "doing"
    assert writes[1][2]["metadata"]["lifecycle_state"] == "active"

    blocked = mod.block_kanban_card({"path": "/kanban/todo/add-mcp-commands", "blocked_reason": "waiting"}, True)
    assert blocked["updated"] is True
    assert writes[2][2]["metadata"]["blocked"] is True
    assert writes[2][2]["metadata"]["blocked_by"] == "waiting"


def test_plan_lifecycle_dry_run_and_write(mod):
    plan = node("/plans/example", "in_progress", tags=["plan"])
    outcome = node("/implementation-notes/example", "completed", node_type="implementation-note")
    writes = []

    def fake_get(args, allow_agent=False):
        if args["path"] == "/plans/example":
            return plan
        if args["path"] == "/implementation-notes/example":
            return outcome
        raise RuntimeError("HTTP 404")

    mod.get_node_by_path = fake_get
    mod.try_get_node_by_path = lambda path, allow_agent=False: outcome if path == "/implementation-notes/example" else None
    mod.update_node_by_path = lambda args: writes.append(("update", args)) or {"node": {**plan, **args}}
    mod.archive_node_by_path = lambda args: writes.append(("archive", args)) or {"archived": True}

    dry = mod.complete_plan({"path": "/plans/example", "outcome_path": "/implementation-notes/example", "dry_run": True})
    assert dry["changed"] is True
    assert dry["dry_run"] is True
    assert dry["status"] == "completed"
    assert "archive node" in dry["planned_changes"]
    assert writes == []

    written = mod.complete_plan({"path": "/plans/example", "outcome_path": "/implementation-notes/example", "reason": "implemented"})
    assert written["changed"] is True
    assert written["archived"] is True
    assert writes[0][0] == "update"
    assert writes[0][1]["metadata"]["status"] == "completed"
    assert writes[0][1]["metadata"]["outcome_path"] == "/implementation-notes/example"
    assert "completed" in writes[0][1]["tags"]
    assert "archived" in writes[0][1]["tags"]
    assert writes[0][1]["links"] == [{"path": "/implementation-notes/example", "relation": "implemented-by"}]
    assert writes[1][0] == "archive"


def test_plan_lifecycle_archived_idempotent(mod):
    archived = node(
        "/plans/example",
        "completed",
        metadata={"outcome_path": "/implementation-notes/example"},
        tags=["plan", "completed", "archived"],
        archived_at="2026-05-06T12:01:00Z",
    )
    archived["links"] = [{"path": "/implementation-notes/example", "relation": "implemented-by"}]

    mod.get_node_by_path = lambda args, allow_agent=False: (_ for _ in ()).throw(RuntimeError("HTTP 404"))
    mod.list_project_nodes = lambda args: {"items": [archived]}
    result = mod.complete_plan({"path": "/plans/example", "outcome_path": "/implementation-notes/example"})
    assert result["ok"] is True
    assert result["changed"] is False
    assert result["archived"] is True


def test_tool_registry(mod):
    names = {tool["name"] for tool in mod.raw_tools()}
    for name in [
        "nap_plan_list_active",
        "nap_kanban_list",
        "nap_kanban_get_pending",
        "nap_kanban_pick_next",
        "nap_kanban_start",
        "nap_kanban_complete",
        "nap_plan_complete",
        "nap_plan_supersede",
        "nap_plan_cancel",
    ]:
        assert name in names
    assert mod.tool_contract_metadata("nap_plan_complete")["dry_run_supported"] is True
    assert mod.tool_contract_metadata("nap_plan_list_active")["side_effect"] == "none"
    assert mod.tool_contract_metadata("nap_kanban_get_pending")["side_effect"] == "none"
    assert mod.tool_contract_metadata("nap_kanban_start")["side_effect"] == "remote-write"


def run():
    mod = load_module()
    test_active_plan_listing(mod)
    mod = load_module()
    test_node_discovery_allows_large_requested_limit(mod)
    mod = load_module()
    test_kanban_list_grouping_and_filters(mod)
    mod = load_module()
    test_kanban_list_reads_beyond_first_source_page(mod)
    mod = load_module()
    test_kanban_workflow_reads_pending_and_pick_next(mod)
    mod = load_module()
    test_kanban_workflow_create_move_and_block(mod)
    mod = load_module()
    test_plan_lifecycle_dry_run_and_write(mod)
    mod = load_module()
    test_plan_lifecycle_archived_idempotent(mod)
    mod = load_module()
    test_tool_registry(mod)
    print("ok: MCP memory ergonomics helpers smoke passed")


if __name__ == "__main__":
    run()
