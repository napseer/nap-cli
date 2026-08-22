import importlib.util
import pathlib
import sys
import threading
import time

import pytest


def load_module():
    script_path = (
        pathlib.Path(__file__).resolve().parents[1]
        / "resources"
        / "scripts"
        / "napseer_mcp_server.py"
    )
    sys.path.insert(0, str(script_path.parent))
    spec = importlib.util.spec_from_file_location("napseer_mcp_batch_test", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    module.DEFAULT_PROJECT_ID = "project-1"
    module.CONFIGURED_TOOL_PROFILES = {"all"}
    return module


@pytest.fixture
def mod(monkeypatch):
    monkeypatch.delenv("NAPSEER_TOOL_PROFILES", raising=False)
    return load_module()


def action(action_id, tool="nap_node_get", *, depends_on=None, arguments=None):
    return {
        "id": action_id,
        "tool": tool,
        "reason": f"Run {action_id} for batch verification",
        "arguments": arguments or {},
        "depends_on": depends_on or [],
    }


def test_batch_is_a_conservatively_annotated_core_tool(mod):
    tool = next(item for item in mod.tools() if item["name"] == "nap_batch")

    assert tool["annotations"] == {
        "title": "nap_batch",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": False,
    }
    assert tool["napseer"]["lock_policy"] == "per-action-mutation-barrier"
    assert tool["napseer"]["idempotency"] == "mixed-no-replay"
    schema = tool["inputSchema"]
    assert schema["properties"]["actions"]["maxItems"] == 32
    assert schema["properties"]["max_parallel"]["maximum"] == 8
    assert set(schema["properties"]["actions"]["items"]["required"]) == {
        "id", "tool", "reason", "arguments",
    }


def test_independent_reads_overlap_and_results_preserve_input_order(mod, monkeypatch):
    state_lock = threading.Lock()
    gate = threading.Barrier(2)
    active = 0
    max_active = 0

    def fake_call(name, arguments):
        nonlocal active, max_active
        with state_lock:
            active += 1
            max_active = max(max_active, active)
        gate.wait(timeout=1)
        if arguments["value"] == "first":
            time.sleep(0.04)
        with state_lock:
            active -= 1
        return {"tool": name, "value": arguments["value"]}

    monkeypatch.setattr(mod, "call_tool_impl", fake_call)
    result = mod.execute_batch({
        "max_parallel": 2,
        "actions": [
            action("first", arguments={"value": "first"}),
            action("second", tool="nap_context", arguments={"value": "second"}),
        ],
    })

    assert result["ok"] is True
    assert result["failure_policy"] == "continue_on_error"
    assert result["parallel_read_groups"] == 1
    assert max_active == 2
    assert [item["id"] for item in result["results"]] == ["first", "second"]
    assert [item["result"]["value"] for item in result["results"]] == ["first", "second"]


def test_mutation_is_an_exclusive_array_order_barrier(mod, monkeypatch):
    state_lock = threading.Lock()
    active_reads = 0
    active_writes = 0
    max_reads = 0
    violations = []
    completed = []

    def fake_call(name, arguments):
        nonlocal active_reads, active_writes, max_reads
        is_read = mod.tool_side_effect(name) == "none"
        with state_lock:
            if is_read:
                if active_writes:
                    violations.append("read overlapped write")
                active_reads += 1
                max_reads = max(max_reads, active_reads)
            else:
                if active_reads or active_writes:
                    violations.append("write was not exclusive")
                active_writes += 1
        time.sleep(0.04)
        with state_lock:
            if is_read:
                active_reads -= 1
            else:
                active_writes -= 1
            completed.append(arguments["value"])
        return {"value": arguments["value"]}

    monkeypatch.setattr(mod, "call_tool_impl", fake_call)
    result = mod.execute_batch({
        "actions": [
            action("read-a", arguments={"value": "read-a"}),
            action("read-b", tool="nap_context", arguments={"value": "read-b"}),
            action("write", tool="nap_create_node", arguments={"value": "write"}),
            action("read-c", arguments={"value": "read-c"}),
            action("read-d", tool="nap_context", arguments={"value": "read-d"}),
        ],
    })

    assert result["ok"] is True
    assert result["failure_policy"] == "stop_on_error"
    assert max_reads == 2
    assert violations == []
    assert completed.index("write") > max(completed.index("read-a"), completed.index("read-b"))
    assert completed.index("write") < min(completed.index("read-c"), completed.index("read-d"))


@pytest.mark.parametrize(
    ("actions", "error_code"),
    [
        (
            [
                action("a", depends_on=["b"]),
                action("b", tool="nap_context", depends_on=["a"]),
            ],
            "batch_dependency_cycle",
        ),
        ([action("a", depends_on=["missing"])], "unknown_batch_dependency"),
        ([action("a", tool="nap_gateway_status")], "batch_tool_not_allowed"),
        ([action("a", tool="nap_batch")], "batch_tool_not_allowed"),
    ],
)
def test_invalid_graphs_and_capabilities_fail_before_execution(
    mod, monkeypatch, actions, error_code
):
    calls = []
    monkeypatch.setattr(mod, "call_tool_impl", lambda *args: calls.append(args))

    with pytest.raises(mod.SafeToolError) as error:
        mod.execute_batch({"actions": actions})

    assert error.value.code == error_code
    assert calls == []


def test_dependency_failure_skips_dependents_but_runs_independent_reads(
    mod, monkeypatch
):
    calls = []

    def fake_call(_name, arguments):
        calls.append(arguments["value"])
        if arguments["value"] == "fails":
            raise mod.SafeToolError("fixture_failure", "fixture failed safely")
        return {"value": arguments["value"]}

    monkeypatch.setattr(mod, "call_tool_impl", fake_call)
    result = mod.execute_batch({
        "failure_policy": "continue_on_error",
        "actions": [
            action("fails", arguments={"value": "fails"}),
            action("dependent", depends_on=["fails"], arguments={"value": "dependent"}),
            action("independent", tool="nap_context", arguments={"value": "independent"}),
        ],
    })

    assert result["ok"] is False
    assert result["stopped_at"] is None
    assert [item["status"] for item in result["results"]] == [
        "failed", "skipped", "succeeded",
    ]
    assert set(calls) == {"fails", "independent"}


def test_stop_on_error_does_not_schedule_a_later_read_wave(mod, monkeypatch):
    calls = []

    def fake_call(_name, arguments):
        calls.append(arguments["value"])
        raise mod.SafeToolError("fixture_failure", "fixture failed safely")

    monkeypatch.setattr(mod, "call_tool_impl", fake_call)
    result = mod.execute_batch({
        "failure_policy": "stop_on_error",
        "max_parallel": 1,
        "actions": [
            action("first", arguments={"value": "first"}),
            action("second", tool="nap_context", arguments={"value": "second"}),
        ],
    })

    assert calls == ["first"]
    assert result["stopped_at"] == "first"
    assert [item["status"] for item in result["results"]] == ["failed", "skipped"]


def test_dry_run_validates_without_invoking_actions(mod, monkeypatch):
    calls = []
    monkeypatch.setattr(mod, "call_tool_impl", lambda *args: calls.append(args))

    result = mod.execute_batch({
        "dry_run": True,
        "actions": [
            action("read", arguments={"node_id": "node-1"}),
            action("write", tool="nap_create_node", arguments={"name": "note"}),
        ],
    })

    assert calls == []
    assert result["failure_policy"] == "dry_run"
    assert result["contains_mutations"] is True
    assert [item["status"] for item in result["results"]] == ["planned", "planned"]


def test_cancellation_prevents_unscheduled_actions_from_starting(mod, monkeypatch):
    cancelled = threading.Event()
    calls = []

    def fake_call(_name, arguments):
        calls.append(arguments["value"])
        cancelled.set()
        return {"ok": True}

    monkeypatch.setattr(mod, "call_tool_impl", fake_call)
    mod.MCP_REQUEST_LOCAL.cancelled = cancelled
    try:
        with pytest.raises(mod.SafeToolError) as error:
            mod.execute_batch({
                "max_parallel": 1,
                "actions": [
                    action("first", arguments={"value": "first"}),
                    action("second", tool="nap_context", arguments={"value": "second"}),
                ],
            })
    finally:
        del mod.MCP_REQUEST_LOCAL.cancelled

    assert error.value.code == "request_cancelled"
    assert calls == ["first"]


def test_protocol_lists_and_dry_runs_batch(mod):
    listed = mod.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = [item["name"] for item in listed["result"]["tools"]]
    assert "nap_batch" in names

    called = mod.handle({
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {
            "name": "nap_batch",
            "arguments": {
                "dry_run": True,
                "actions": [action("read", arguments={"node_id": "node-1"})],
            },
        },
    })
    payload = called["result"]["structuredContent"]
    assert called["result"]["isError"] is False
    assert payload["ok"] is True
    assert payload["results"][0]["status"] == "planned"


def test_profile_gated_tool_is_rejected_before_execution(mod, monkeypatch):
    mod.CONFIGURED_TOOL_PROFILES = set()
    calls = []
    monkeypatch.setattr(mod, "call_tool_impl", lambda *args: calls.append(args))

    with pytest.raises(mod.SafeToolError) as error:
        mod.execute_batch({"actions": [action("plan", tool="nap_plan_list_active")]})

    assert error.value.code == "tool_profile_required"
    assert calls == []
