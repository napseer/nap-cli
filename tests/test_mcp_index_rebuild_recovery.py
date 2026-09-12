"""Public memory-tool regressions with disposable SQLite and an HTTP provider."""

import copy
import importlib.util
import json
import pathlib
import sqlite3
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest


def node(name, minute, *, archived=False):
    stamp = f"2026-09-01T12:{minute:02d}:00Z"
    return {
        "id": name, "project_id": "project-1", "full_path": f"/notes/{name}",
        "folder_path": "/notes", "name": name, "type": "custom-existing-type",
        "metadata": {"custom": "retained"}, "tags": [], "aliases": [], "links": [],
        "content_text": name, "updated_at": stamp,
        "archived_at": stamp if archived else None,
    }


@pytest.fixture
def memory(tmp_path, monkeypatch):
    provider = {"nodes": [], "requests": [], "fail_page": None, "override": None}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            query = parse_qs(urlsplit(self.path).query)
            provider["requests"].append(query)
            offset = int(query.get("cursor", ["0"])[0])
            if offset == provider["fail_page"] or len(provider["requests"]) > 20:
                self.send_response(503)
                self.end_headers()
                self.wfile.write(b'{"error":{"code":"unavailable","message":"test outage"}}')
                return
            rows = copy.deepcopy(provider["nodes"])
            after = query.get("updated_after", [""])[0]
            before = query.get("updated_before", [""])[0]
            rows = [n for n in rows if n["updated_at"] > after
                    and (not before or n["updated_at"] < before)
                    and (query.get("include_archived") == ["true"] or not n["archived_at"])]
            rows.sort(key=lambda n: (n["updated_at"], n["id"]), reverse=True)
            limit = int(query.get("limit", ["200"])[0])
            end = offset + limit
            page = {"items": rows[offset:end], "next_cursor": str(end) if end < len(rows) else None}
            if provider["override"] is not None:
                page = provider["override"](page, query)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(page).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("NAPSEER_TOOL_PROFILES", "all")
    script = pathlib.Path(__file__).resolve().parents[1] / "resources/scripts/napseer_mcp_server.py"
    monkeypatch.syspath_prepend(str(script.parent))

    def worker():
        spec = importlib.util.spec_from_file_location("rebuild_recovery_test_worker", script)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.AUTH_DIR = tmp_path / "project-state"
        mod.INDEX_PATH = mod.AUTH_DIR / "index.sqlite"
        mod.INDEX_LOCK_PATH = mod.AUTH_DIR / "index.lock"
        mod.BASE_URL = f"http://127.0.0.1:{server.server_port}"
        mod.TOKEN = "disposable-test-token"
        mod.AUTH = {"account_id": "test-account"}
        mod.DEFAULT_PROJECT_ID = "project-1"
        mod.CONFIGURED_TOOL_PROFILES = {"all"}
        # Identity is synthetic; the real request, dispatch and index code run.
        mod.refresh_public_auth_state = lambda: None
        mod.require_unlocked = lambda _name: None
        mod.resolve_project_id = lambda args: args.get("project_id", "project-1")
        return mod

    try:
        yield worker(), provider, worker
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def rebuild(mod, capsys, *, limit=200):
    mod.cli_main(["napseer_mcp_server.py", "reindex", "--limit", str(limit)])
    return json.loads(capsys.readouterr().out)


def found(mod, text):
    return [n["id"] for n in mod.call_tool_impl(
        "nap_discover", {"q": text, "mode": "local"}
    )["items"]]


def seed(mod, provider, capsys):
    provider["nodes"] = [node("initial", 0)]
    rebuild(mod, capsys)


def test_rebuild_finds_remote_changes_despite_newer_cached_write(memory, capsys):
    mod, provider, _ = memory
    seed(mod, provider, capsys)
    remote, own = node("remoteunique", 5), node("ownwrite", 10)
    provider["nodes"].extend([remote, own])
    mod.index_node(own)

    result = rebuild(mod, capsys)

    assert result["graph_complete"] is True
    assert found(mod, "remoteunique") == ["remoteunique"]


def test_failed_page_preserves_snapshot_and_restart_recovers(memory, capsys):
    mod, provider, worker = memory
    seed(mod, provider, capsys)
    provider["nodes"].extend([node("remoteunique", 5), node("newest", 20)])
    provider["fail_page"] = 1
    with pytest.raises(mod.SafeToolError):
        rebuild(mod, capsys, limit=1)
    assert found(mod, "newest") == []
    own = node("ownwrite", 30)
    mod.index_node(own)
    provider["nodes"].append(own)
    provider["fail_page"] = None

    restarted = worker()
    rebuild(restarted, capsys, limit=1)

    assert found(restarted, "remoteunique") == ["remoteunique"]


def test_existing_records_need_no_new_fields_to_rebuild(memory, capsys):
    mod, provider, _ = memory
    mod.index_node(node("ownwrite", 30))
    mod.set_local_graph_index_complete("project-1", True)
    provider["nodes"] = [node("remoteunique", 5), node("ownwrite", 30)]

    rebuild(mod, capsys)

    assert "updated_after" not in provider["requests"][-1]
    assert found(mod, "remoteunique") == ["remoteunique"]


def test_apply_failure_rolls_back_batch_and_retry_recovers(memory, capsys):
    mod, provider, _ = memory
    seed(mod, provider, capsys)
    provider["nodes"].extend([node("newest", 20), node("failsave", 5)])
    with mod.index_connect() as conn:
        conn.execute("CREATE TRIGGER fail_sync BEFORE INSERT ON local_index_nodes "
                     "WHEN NEW.node_id = 'failsave' BEGIN SELECT RAISE(ABORT, 'injected'); END")
    with pytest.raises(sqlite3.IntegrityError):
        rebuild(mod, capsys)
    assert found(mod, "newest") == []
    assert found(mod, "initial") == ["initial"]
    assert mod.local_index_diagnostics("project-1")["graph_complete"] is True
    with mod.index_connect() as conn:
        conn.execute("DROP TRIGGER fail_sync")

    rebuild(mod, capsys)
    assert found(mod, "failsave") == ["failsave"]


@pytest.mark.parametrize("page", [{}, {"items": {}}, {"items": [], "next_cursor": 3},
                                   {"items": [None]}, {"items": [], "has_more": True},
                                   {"items": [{"id": "broken"}]},
                                   {"items": [{**node("other", 5), "project_id": "project-2"}]}])
def test_malformed_rebuild_page_preserves_index_and_returns_safe_failure(memory, capsys, page):
    mod, provider, _ = memory
    seed(mod, provider, capsys)
    provider["override"] = lambda *_: page

    with pytest.raises(mod.SafeToolError) as error:
        rebuild(mod, capsys)

    assert error.value.code == "invalid_index_response"
    assert found(mod, "initial") == ["initial"]


def test_repeated_cursor_is_rejected_before_mutating_the_index(memory, capsys):
    mod, provider, _ = memory
    seed(mod, provider, capsys)
    provider["override"] = lambda *_: {"items": [], "next_cursor": "1"}

    with pytest.raises(mod.SafeToolError) as error:
        rebuild(mod, capsys)

    assert error.value.code == "invalid_index_response"
    assert len(provider["requests"]) == 3  # seed + two pages, no unbounded loop
    assert found(mod, "initial") == ["initial"]


def test_readers_see_previous_snapshot_until_replacement_commits(memory, capsys, monkeypatch):
    mod, provider, _ = memory
    seed(mod, provider, capsys)
    provider["nodes"] = [node("replacement", 20), node("second", 10)]
    entered, release = threading.Event(), threading.Event()
    errors = []
    original = mod.index_node

    def paused_write(item, **kwargs):
        original(item, **kwargs)
        if item["id"] == "replacement":
            entered.set()
            assert release.wait(timeout=3)

    def run():
        try:
            mod.cli_main(["napseer_mcp_server.py", "reindex"])
        except Exception as error:
            errors.append(error)

    monkeypatch.setattr(mod, "index_node", paused_write)
    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert entered.wait(timeout=3)
        assert found(mod, "initial") == ["initial"]
        assert found(mod, "replacement") == []
        assert mod.local_index_diagnostics("project-1")["graph_complete"] is True
    finally:
        release.set()
        thread.join(timeout=3)

    assert not thread.is_alive()
    assert not errors
    assert json.loads(capsys.readouterr().out)["graph_complete"] is True
    assert found(mod, "initial") == []
    assert found(mod, "replacement") == ["replacement"]


def test_cancelled_replacement_rolls_back_and_releases_writer_lock(memory, capsys, monkeypatch):
    mod, provider, _ = memory
    seed(mod, provider, capsys)
    provider["nodes"] = [node("replacement", 20), node("second", 10)]
    cancelled = threading.Event()
    mod.MCP_REQUEST_LOCAL.cancelled = cancelled
    original = mod.index_node

    def cancel_after_write(item, **kwargs):
        original(item, **kwargs)
        cancelled.set()

    monkeypatch.setattr(mod, "index_node", cancel_after_write)
    with pytest.raises(mod.SafeToolError) as error:
        rebuild(mod, capsys)
    assert error.value.code == "request_cancelled"
    cancelled.clear()
    assert found(mod, "initial") == ["initial"]
    assert found(mod, "replacement") == []
    assert mod.local_index_diagnostics("project-1")["graph_complete"] is True

    monkeypatch.setattr(mod, "index_node", original)
    rebuild(mod, capsys)
    assert found(mod, "replacement") == ["replacement"]


def test_rebuild_preserves_other_projects_and_existing_node_facets(memory, capsys):
    mod, provider, _ = memory
    seed(mod, provider, capsys)
    mod.index_node({**node("otherproject", 30), "project_id": "project-2"})
    kept = node("kept", 5)
    kept.update(tags=["existing-tag"], aliases=["secondaryhandle"],
                links=[{"path": "/notes/target", "relation": "custom-relation"}])
    mod.index_node(kept)
    assert found(mod, "secondaryhandle") == ["kept"], "baseline before rebuild"
    provider["nodes"] = [kept, node("target", 6), node("retired", 8, archived=True)]

    result = rebuild(mod, capsys, limit=1)

    assert result["indexed"] == 2
    assert found(mod, "initial") == []
    assert found(mod, "retired") == []
    assert found(mod, "secondaryhandle") == ["kept"]
    with mod.index_connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM local_index_nodes WHERE project_id='project-2'").fetchone()[0] == 1
        assert conn.execute("SELECT node_type FROM local_index_nodes WHERE node_id='kept'").fetchone()[0] == "custom-existing-type"
        assert json.loads(conn.execute("SELECT aliases_json FROM local_index_nodes WHERE node_id='kept'").fetchone()[0]) == ["secondaryhandle"]
        assert conn.execute("SELECT relation FROM local_index_edges WHERE source_node_id='kept'").fetchone()[0] == "custom-relation"
        assert conn.execute("SELECT tag FROM local_index_node_tags WHERE node_id='kept'").fetchone()[0] == "existing-tag"


def test_valid_empty_inventory_commits_an_empty_complete_project(memory, capsys):
    mod, provider, _ = memory
    seed(mod, provider, capsys)
    provider["nodes"] = []

    result = rebuild(mod, capsys)

    assert result["indexed"] == 0
    assert result["graph_complete"] is True
    assert mod.local_index_diagnostics("project-1")["count"] == 0


def test_process_exit_during_rebuild_preserves_snapshot_after_restart(memory, capsys):
    mod, provider, worker = memory
    seed(mod, provider, capsys)
    provider["nodes"] = [node("replacement", 20), node("second", 10)]
    child = r'''
import importlib.util, os, pathlib, sys
script, state, base = sys.argv[1:]
sys.path.insert(0, str(pathlib.Path(script).parent))
spec = importlib.util.spec_from_file_location("rebuild_crash_worker", script)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
mod.AUTH_DIR = pathlib.Path(state)
mod.INDEX_PATH = mod.AUTH_DIR / "index.sqlite"
mod.INDEX_LOCK_PATH = mod.AUTH_DIR / "index.lock"
mod.BASE_URL = base
mod.TOKEN = "disposable-test-token"
mod.DEFAULT_PROJECT_ID = "project-1"
mod.refresh_public_auth_state = lambda: None
mod.resolve_project_id = lambda _: "project-1"
original = mod.index_node
def crash_after_write(item, **kwargs):
    original(item, **kwargs)
    os._exit(71)
mod.index_node = crash_after_write
mod.cli_main([script, "reindex"])
'''
    result = subprocess.run(
        [sys.executable, "-c", child, mod.__file__, str(mod.AUTH_DIR), mod.BASE_URL],
        capture_output=True, timeout=8, check=False,
    )
    assert result.returncode == 71
    restarted = worker()
    assert found(restarted, "initial") == ["initial"]
    assert found(restarted, "replacement") == []
    assert restarted.local_index_diagnostics("project-1")["graph_complete"] is True
    rebuild(restarted, capsys)
    assert found(restarted, "replacement") == ["replacement"]
