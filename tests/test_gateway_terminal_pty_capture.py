import importlib.util
import pathlib
import types


def load_module():
    script_path = pathlib.Path(__file__).resolve().parents[1] / "resources" / "scripts" / "napseer_mcp_server.py"
    spec = importlib.util.spec_from_file_location("napseer_mcp_server_pty_capture_test", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_capture_reads_pty_ring_instead_of_tmux():
    mod = load_module()
    attach = types.SimpleNamespace(
        chunks=(
            types.SimpleNamespace(data=b"first\n"),
            types.SimpleNamespace(data=b"napseer-lifecycle-ok\n"),
        ),
        newest_seq=2,
        replay_gap=False,
    )
    manager = types.SimpleNamespace(attach=lambda terminal_id: attach)
    mod.gateway_terminal_backend_from_id = lambda terminal_id, remote_authenticated=False: (
        "pty",
        {"id": terminal_id},
    )
    mod.gateway_pty_manager = lambda: manager

    result = mod.gateway_terminal_capture({"terminal_id": "term_test"})

    assert result["status"] == "captured"
    assert result["terminal_backend"] == "pty"
    assert result["terminal_id"] == "term_test"
    assert "napseer-lifecycle-ok" in result["output"]
    assert result["output_seq"] == 2
