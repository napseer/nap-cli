import importlib.util
import pathlib
import sys
import threading
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
        "napseer_mcp_server_lifecycle_test", script_path
    )
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def wait_until(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not reached before timeout")


def test_worker_runtime_completes_fast_read_before_slow_read():
    mod = load_module()
    emitted = []
    emitted_lock = threading.Lock()

    def handler(message):
        if message["id"] == 1:
            time.sleep(0.4)
        return mod.rpc_result(message["id"], {"done": True})

    def emit(response):
        with emitted_lock:
            emitted.append(response)

    runtime = mod.McpRequestRuntime(handler=handler, sender=emit, max_workers=4)
    try:
        runtime.dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/call"})
        runtime.dispatch({"jsonrpc": "2.0", "id": 2, "method": "tools/call"})

        wait_until(lambda: len(emitted) == 2)

        assert [response["id"] for response in emitted] == [2, 1]
    finally:
        runtime.close()


def test_worker_runtime_cancellation_suppresses_unused_result():
    mod = load_module()
    emitted = []

    def handler(message):
        if message["id"] == 1:
            time.sleep(0.3)
        return mod.rpc_result(message["id"], {"done": True})

    runtime = mod.McpRequestRuntime(handler=handler, sender=emitted.append, max_workers=2)
    try:
        runtime.dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/call"})
        runtime.dispatch(
            {
                "jsonrpc": "2.0",
                "method": "notifications/cancelled",
                "params": {"requestId": 1, "reason": "unused"},
            }
        )
        runtime.dispatch({"jsonrpc": "2.0", "id": 2, "method": "tools/call"})

        wait_until(lambda: any(response["id"] == 2 for response in emitted))
        time.sleep(0.35)

        assert [response["id"] for response in emitted] == [2]
    finally:
        runtime.close()


def test_initialize_cannot_be_cancelled():
    mod = load_module()
    emitted = []

    def handler(message):
        time.sleep(0.1)
        return mod.rpc_result(message["id"], {"initialized": True})

    runtime = mod.McpRequestRuntime(handler=handler, sender=emitted.append, max_workers=2)
    try:
        runtime.dispatch({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        runtime.dispatch(
            {
                "jsonrpc": "2.0",
                "method": "notifications/cancelled",
                "params": {"requestId": 1},
            }
        )

        wait_until(lambda: len(emitted) == 1)

        assert emitted[0]["id"] == 1
    finally:
        runtime.close()


def test_unknown_notification_never_returns_a_response():
    mod = load_module()

    response = mod.handle(
        {"jsonrpc": "2.0", "method": "notifications/unknown", "params": {}}
    )

    assert response is None


def test_mutating_tools_are_serialized_while_reads_can_overlap(monkeypatch):
    mod = load_module()
    active = 0
    max_active = 0
    state_lock = threading.Lock()

    def fake_call(_name, _args):
        nonlocal active, max_active
        with state_lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.1)
        with state_lock:
            active -= 1
        return {"ok": True}

    monkeypatch.setattr(mod, "call_tool_impl", fake_call)

    read_threads = [
        threading.Thread(target=mod.call_tool, args=("nap_whoami", {}))
        for _ in range(2)
    ]
    for thread in read_threads:
        thread.start()
    for thread in read_threads:
        thread.join()
    assert max_active == 2

    max_active = 0
    write_threads = [
        threading.Thread(target=mod.call_tool, args=("nap_create_node", {}))
        for _ in range(2)
    ]
    for thread in write_threads:
        thread.start()
    for thread in write_threads:
        thread.join()
    assert max_active == 1


def test_concurrent_first_reads_share_one_project_bootstrap(tmp_path, monkeypatch):
    mod = load_module()
    mod.AUTH_PATH = tmp_path / "missing-auth.json"
    mod.DEFAULT_PROJECT_ID = None
    monkeypatch.setattr(mod, "refresh_public_auth_state", lambda: None)
    calls = []

    def bootstrap(_args):
        calls.append("bootstrap")
        time.sleep(0.1)
        mod.DEFAULT_PROJECT_ID = "project-1"
        return {"project_id": "project-1"}

    monkeypatch.setattr(mod, "bootstrap_project", bootstrap)
    results = []
    threads = [
        threading.Thread(target=lambda: results.append(mod.resolve_project_id({})))
        for _ in range(3)
    ]

    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results == ["project-1"] * 3
    assert calls == ["bootstrap"]


def test_concurrent_auth_updates_are_atomic_and_merge_fields(tmp_path):
    mod = load_module()
    mod.AUTH_DIR = tmp_path / ".napseer"
    mod.AUTH_PATH = mod.AUTH_DIR / "auth.json"
    mod.VAULT_PATH = mod.AUTH_DIR / "vault.json"
    mod.AUTH_DIR.mkdir()
    mod.write_public_auth({"base_url": "https://api.example.test"})
    barrier = threading.Barrier(3)

    def update(values):
        barrier.wait()
        mod.save_auth(values)

    threads = [
        threading.Thread(target=update, args=({"project_slug": "demo"},)),
        threading.Thread(target=update, args=({"project_name": "Demo"},)),
    ]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    saved = mod.load_public_auth_file()
    assert saved["project_slug"] == "demo"
    assert saved["project_name"] == "Demo"
    assert not list(mod.AUTH_DIR.glob(".auth.json.*.tmp"))


def test_auth_renewal_reuses_token_refreshed_by_another_request(monkeypatch):
    mod = load_module()
    mod.TOKEN = "fresh-access"
    mod.TOKEN_EXPIRES_AT = "future"
    mod.REFRESH_EXPIRES_AT = "later"
    mod.DEFAULT_PROJECT_ID = "project-1"
    monkeypatch.setattr(mod, "refresh_public_auth_state", lambda: None)
    monkeypatch.setattr(
        mod,
        "_renew_auth_locked",
        lambda: (_ for _ in ()).throw(AssertionError("must not rotate again")),
    )

    result = mod.renew_auth(stale_token="stale-access")

    assert result == {
        "status": "already_renewed",
        "method": "concurrent_refresh",
        "token_expires_at": "future",
        "refresh_expires_at": "later",
        "project_id": "project-1",
    }
