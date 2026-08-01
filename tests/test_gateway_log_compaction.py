import importlib.util
import pathlib


def load_module():
    script_path = pathlib.Path(__file__).resolve().parents[1] / "resources" / "scripts" / "napseer_mcp_server.py"
    spec = importlib.util.spec_from_file_location("napseer_gateway_log_compaction_test", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_log_compaction_preserves_live_writer_inode(tmp_path):
    mod = load_module()
    log_path = tmp_path / "gateway.log"
    log_path.write_bytes((b"old-line\n" * 200) + b"recent-line\n")
    inode_before = log_path.stat().st_ino

    with log_path.open("ab", buffering=0) as live_writer:
        mod.compact_log_file(log_path, 128)
        live_writer.write(b"after-compaction\n")

    assert log_path.stat().st_ino == inode_before
    content = log_path.read_bytes()
    assert content.endswith(b"after-compaction\n")
    assert b"recent-line\n" in content
