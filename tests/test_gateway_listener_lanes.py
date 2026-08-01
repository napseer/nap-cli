import importlib.util
import pathlib


def load_module():
    script_path = pathlib.Path(__file__).resolve().parents[1] / "resources" / "scripts" / "napseer_mcp_server.py"
    spec = importlib.util.spec_from_file_location("napseer_gateway_listener_lanes_test", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_gateway_relay_uses_one_listener_lane_by_default(monkeypatch):
    mod = load_module()
    monkeypatch.delenv("NAPSEER_GATEWAY_RELAY_LISTENER_MIN_LANES", raising=False)
    monkeypatch.delenv("NAPSEER_GATEWAY_RELAY_LISTENER_MAX_LANES", raising=False)

    assert mod.gateway_listener_min_lanes() == 1
    assert mod.gateway_listener_max_lanes() == 1
    assert mod.gateway_listener_target_lanes() == 1
    assert mod.gateway_listener_lane_ids() == ["lane-1"]


def test_gateway_relay_extra_lanes_require_explicit_configuration(monkeypatch):
    mod = load_module()
    monkeypatch.setenv("NAPSEER_GATEWAY_RELAY_LISTENER_MIN_LANES", "1")
    monkeypatch.setenv("NAPSEER_GATEWAY_RELAY_LISTENER_MAX_LANES", "3")

    assert mod.gateway_listener_lane_ids() == ["lane-1", "lane-2", "lane-3"]
