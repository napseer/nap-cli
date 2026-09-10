"""Exercise the published `curl /install | python3 -` entry point over HTTP."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def test_stdin_bootstrap_installs_verified_runtime_and_preserves_credentials(tmp_path):
    source = Path(__file__).resolve().parents[1] / "resources/scripts"
    spec = importlib.util.spec_from_file_location("bootstrap_contract", source / "nap_install.py")
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    revision = "f" * 40
    items, payloads = [], {}
    for name in installer.SCRIPT_NAMES:
        content = (source / installer.INSTALL_PATHS[name]).read_text()
        item = {"name": name, "version": installer.CLI_RELEASE_VERSION,
                "bytes": len(content.encode()), "sha256": hashlib.sha256(content.encode()).hexdigest(),
                "source_repo": "https://github.com/napseer/nap-cli", "source_revision": revision,
                "source_revision_status": "resolved", "contract_version": installer.CLI_DISTRIBUTION_CONTRACT_VERSION,
                "minimum_contract_version": installer.CLI_MINIMUM_CONTRACT_VERSION,
                "install_path": installer.INSTALL_PATHS[name], "mode": "0755"}
        items.append(item)
        payloads["/v1/scripts/" + name] = {**item, "content": content}
    payloads["/v1/scripts"] = {"schema_version": installer.CLI_BUNDLE_SCHEMA_VERSION,
        "bundle_id": "synthetic-bootstrap", "release_version": installer.CLI_RELEASE_VERSION,
        "contract": {"current": installer.CLI_DISTRIBUTION_CONTRACT_VERSION, "minimum_supported": installer.CLI_MINIMUM_CONTRACT_VERSION},
        "source": {"repo": "https://github.com/napseer/nap-cli", "revision": revision, "revision_status": "resolved"}, "items": items}

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            assert self.path in payloads, "bootstrap must only download its declared bundle"
            raw = json.dumps(payloads[self.path]).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    user_data = tmp_path / "user-data"
    user_data.mkdir(exist_ok=True)
    auth = user_data / "auth.json"
    auth.write_text('{"refresh_token":"synthetic-preserved"}')
    env = {"PATH": os.environ["PATH"], "NAPSEER_HOME": str(tmp_path / "runtime"),
        "NAPSEER_BIN_DIR": str(tmp_path / "bin"), "NAPSEER_USER_DATA_DIR": str(user_data),
        "NAPSEER_PROJECT_ROOT": str(tmp_path), "NAPSEER_BASE_URL": f"http://127.0.0.1:{server.server_port}"}
    try:
        result = subprocess.run([sys.executable, "-"], input=(source / "nap_install.py").read_text(),
            cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, "the advertised stdin bootstrap must install without an explicit subcommand"
        assert json.loads(result.stdout)["status"] == "installed"
        for name in installer.SCRIPT_NAMES:
            installed = tmp_path / "runtime/current" / installer.INSTALL_PATHS[name]
            assert installed.read_bytes() == (source / installer.INSTALL_PATHS[name]).read_bytes()
        assert auth.read_text() == '{"refresh_token":"synthetic-preserved"}'
        # Import the actual activated modules, including the newly added dependency.
        env["PYTHONPATH"] = str(tmp_path / "runtime/current")
        imported = subprocess.run([sys.executable, "-c", "import napseer_credentials, napseer_mcp_server"],
            cwd=tmp_path, env=env, capture_output=True, timeout=10)
        assert imported.returncode == 0, "activated runtime must load all dependencies"
        result = subprocess.run([sys.executable, str(tmp_path / "bin/nap"), "update"],
            cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0
        assert auth.read_text() == '{"refresh_token":"synthetic-preserved"}'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
