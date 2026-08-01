import importlib.util
import pathlib
import threading
import time


def load_module():
    script_path = pathlib.Path(__file__).resolve().parents[1] / "resources" / "scripts" / "napseer_mcp_server.py"
    spec = importlib.util.spec_from_file_location("napseer_mcp_server_gateway_auth_test", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_renew_auth_serializes_rotating_refresh_tokens():
    mod = load_module()
    active = 0
    maximum = 0
    guard = threading.Lock()

    def fake_refresh():
        nonlocal active, maximum
        with guard:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.03)
        with guard:
            active -= 1
        return {"status": "renewed"}

    mod.refresh_public_auth_state = lambda: None
    mod._renew_auth_locked = fake_refresh
    threads = [threading.Thread(target=mod.renew_auth) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert maximum == 1


def test_gateway_capability_update_does_not_refresh_by_default():
    mod = load_module()
    mod.AUTH = {"worker_capabilities": {}}
    calls = []
    mod.save_auth = lambda updates: mod.AUTH.update(updates)
    mod.renew_auth = lambda: calls.append("renewed")

    result = mod.ensure_gateway_worker_capability(refresh=False)

    assert result["status"] == "updated"
    assert calls == []
