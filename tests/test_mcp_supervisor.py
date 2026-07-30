import json
import os
import pathlib
import select
import signal
import subprocess
import sys
import time


ROOT = pathlib.Path(__file__).resolve().parents[1]
SUPERVISOR = ROOT / "resources" / "scripts" / "napseer_mcp_supervisor.py"


def write_worker(path: pathlib.Path, version: int) -> None:
    path.write_text(
        f"""\
import json
import os
import sys

VERSION = {version}

for line in sys.stdin:
    message = json.loads(line)
    if message.get("method") == "notifications/initialized":
        continue
    if message.get("method") == "tools/call":
        name = (message.get("params") or {{}}).get("name")
        if name == "crash":
            os._exit(17)
        result = {{"pid": os.getpid(), "version": VERSION, "name": name}}
    else:
        result = {{"version": VERSION}}
    print(json.dumps({{"jsonrpc": "2.0", "id": message.get("id"), "result": result}}), flush=True)
""",
        encoding="utf-8",
    )


class SupervisorClient:
    def __init__(self, worker_path: pathlib.Path):
        environment = os.environ.copy()
        environment["NAPSEER_MCP_WORKER_PATH"] = str(worker_path)
        environment["NAPSEER_MCP_RESPONSE_TIMEOUT_SECONDS"] = "5"
        self.process = subprocess.Popen(
            [sys.executable, str(SUPERVISOR)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
        )

    def request(self, request_id: int, method: str, params=None):
        assert self.process.stdin is not None
        assert self.process.stdout is not None
        message = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        self.process.stdin.write(json.dumps(message).encode("utf-8") + b"\n")
        self.process.stdin.flush()
        readable, _, _ = select.select([self.process.stdout], [], [], 8)
        assert readable, "supervisor did not return a response"
        return json.loads(self.process.stdout.readline())

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            self.process.wait(timeout=5)


def test_restarts_worker_killed_between_requests(tmp_path):
    worker_path = tmp_path / "worker.py"
    write_worker(worker_path, 1)
    client = SupervisorClient(worker_path)
    try:
        first = client.request(1, "tools/call", {"name": "pid", "arguments": {}})
        os.kill(first["result"]["pid"], signal.SIGTERM)
        time.sleep(0.1)

        second = client.request(2, "tools/list")

        assert second["result"]["version"] == 1
        assert client.process.poll() is None
    finally:
        client.close()


def test_reloads_replaced_worker_between_requests(tmp_path):
    worker_path = tmp_path / "worker.py"
    write_worker(worker_path, 1)
    client = SupervisorClient(worker_path)
    try:
        assert client.request(1, "initialize")["result"]["version"] == 1
        time.sleep(0.01)
        write_worker(worker_path, 2)
        os.utime(worker_path, None)

        assert client.request(2, "tools/list")["result"]["version"] == 2
    finally:
        client.close()


def test_worker_crash_returns_error_without_closing_transport(tmp_path):
    worker_path = tmp_path / "worker.py"
    write_worker(worker_path, 1)
    client = SupervisorClient(worker_path)
    try:
        failed = client.request(1, "tools/call", {"name": "crash", "arguments": {}})
        recovered = client.request(2, "tools/list")

        assert failed["error"]["code"] == -32098
        assert recovered["result"]["version"] == 1
        assert client.process.poll() is None
    finally:
        client.close()
