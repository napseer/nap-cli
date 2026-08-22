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
REAL_WORKER = ROOT / "resources" / "scripts" / "napseer_mcp_server.py"


def write_worker(path: pathlib.Path, version: int) -> None:
    path.write_text(
        f"""\
import json
import os
import sys
import threading
import time

VERSION = {version}
WRITE_LOCK = threading.Lock()
CANCELLED = set()


def emit(payload):
    with WRITE_LOCK:
        print(json.dumps(payload), flush=True)


def handle(message):
    method = message.get("method")
    if method == "notifications/initialized":
        return
    if method == "notifications/cancelled":
        CANCELLED.add((message.get("params") or {{}}).get("requestId"))
        return
    if method == "notifications/noisy":
        emit({{"jsonrpc": "2.0", "id": None, "error": {{"code": -32601, "message": "noise"}}}})
        return
    if "id" not in message:
        return
    if method == "tools/call":
        name = (message.get("params") or {{}}).get("name")
        if name == "slow":
            time.sleep(1)
        if name == "crash":
            time.sleep(0.1)
            os._exit(17)
        result = {{"pid": os.getpid(), "version": VERSION, "name": name}}
    else:
        result = {{"version": VERSION}}
    if message.get("id") in CANCELLED:
        return
    emit({{"jsonrpc": "2.0", "id": message.get("id"), "result": result}})

for line in sys.stdin:
    message = json.loads(line)
    if "id" not in message:
        handle(message)
        continue
    threading.Thread(target=handle, args=(message,), daemon=True).start()
""",
        encoding="utf-8",
    )


class SupervisorClient:
    def __init__(
        self,
        worker_path: pathlib.Path,
        project_root: pathlib.Path | None = None,
        watch_paths: tuple[pathlib.Path, ...] = (),
    ):
        environment = os.environ.copy()
        environment["NAPSEER_MCP_WORKER_PATH"] = str(worker_path)
        if watch_paths:
            environment["NAPSEER_MCP_WORKER_WATCH_PATHS"] = os.pathsep.join(
                str(path) for path in watch_paths
            )
        environment["NAPSEER_MCP_RESPONSE_TIMEOUT_SECONDS"] = "5"
        environment["NAPSEER_TELEMETRY"] = "0"
        if project_root is not None:
            environment["NAPSEER_PROJECT_ROOT"] = str(project_root)
        self.process = subprocess.Popen(
            [sys.executable, str(SUPERVISOR)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
        )

    def request(self, request_id: int, method: str, params=None):
        self.send(request_id, method, params)
        return self.receive()

    def send(self, request_id: int, method: str, params=None):
        assert self.process.stdin is not None
        message = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        self.process.stdin.write(json.dumps(message).encode("utf-8") + b"\n")
        self.process.stdin.flush()

    def notify(self, method: str, params=None):
        assert self.process.stdin is not None
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self.process.stdin.write(json.dumps(message).encode("utf-8") + b"\n")
        self.process.stdin.flush()

    def receive(self, timeout=8):
        assert self.process.stdout is not None
        readable, _, _ = select.select([self.process.stdout], [], [], timeout)
        assert readable, "supervisor did not return a response"
        return json.loads(self.process.stdout.readline())

    def close(self):
        if self.process.poll() is None:
            assert self.process.stdin is not None
            self.process.stdin.close()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
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


def test_concurrent_requests_are_correlated_without_head_of_line_blocking(tmp_path):
    worker_path = tmp_path / "worker.py"
    write_worker(worker_path, 1)
    client = SupervisorClient(worker_path)
    try:
        client.send(1, "tools/call", {"name": "slow", "arguments": {}})
        time.sleep(0.05)
        client.send(2, "tools/call", {"name": "fast", "arguments": {}})

        first = client.receive(timeout=0.75)
        second = client.receive(timeout=2)

        assert first["id"] == 2
        assert first["result"]["name"] == "fast"
        assert second["id"] == 1
        assert second["result"]["name"] == "slow"
    finally:
        client.close()


def test_notification_response_cannot_poison_the_next_request(tmp_path):
    worker_path = tmp_path / "worker.py"
    write_worker(worker_path, 1)
    client = SupervisorClient(worker_path)
    try:
        client.notify("notifications/noisy")
        time.sleep(0.05)

        response = client.request(7, "tools/list")

        assert response["id"] == 7
        assert response["result"]["version"] == 1
    finally:
        client.close()


def test_cancellation_drops_the_cancelled_and_late_worker_response(tmp_path):
    worker_path = tmp_path / "worker.py"
    write_worker(worker_path, 1)
    client = SupervisorClient(worker_path)
    try:
        client.send(1, "tools/call", {"name": "slow", "arguments": {}})
        time.sleep(0.05)
        client.notify(
            "notifications/cancelled",
            {"requestId": 1, "reason": "test cancellation"},
        )
        client.send(2, "tools/call", {"name": "fast", "arguments": {}})

        response = client.receive(timeout=0.75)
        assert response["id"] == 2

        time.sleep(1.05)
        follow_up = client.request(3, "tools/list")
        assert follow_up["id"] == 3
    finally:
        client.close()


def test_worker_crash_fails_every_in_flight_request_once_then_recovers(tmp_path):
    worker_path = tmp_path / "worker.py"
    write_worker(worker_path, 1)
    client = SupervisorClient(worker_path)
    try:
        client.send(1, "tools/call", {"name": "slow", "arguments": {}})
        client.send(2, "tools/call", {"name": "crash", "arguments": {}})

        failed = [client.receive(timeout=2), client.receive(timeout=2)]

        assert {item["id"] for item in failed} == {1, 2}
        assert all(item["error"]["code"] == -32098 for item in failed)

        recovered = client.request(3, "tools/list")
        assert recovered["id"] == 3
        assert recovered["result"]["version"] == 1
        assert client.process.poll() is None
    finally:
        client.close()


def test_real_supervisor_worker_protocol_lifecycle(tmp_path):
    client = SupervisorClient(REAL_WORKER, project_root=tmp_path)
    try:
        initialized = client.request(
            1,
            "initialize",
            {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "protocol-test", "version": "1"},
            },
        )
        client.notify("notifications/initialized", {})
        client.send(2, "tools/list")
        client.send(3, "tools/call", {"name": "nap_whoami", "arguments": {}})

        responses = {item["id"]: item for item in (client.receive(), client.receive())}

        assert initialized["result"]["protocolVersion"] == "2025-11-25"
        tool_names = {tool["name"] for tool in responses[2]["result"]["tools"]}
        assert "nap_whoami" in tool_names
        assert responses[3]["result"]["isError"] is False

        client.notify("notifications/unknown", {"proof": "one-way"})
        follow_up = client.request(
            4,
            "tools/call",
            {"name": "nap_whoami", "arguments": {}},
        )
        assert follow_up["id"] == 4
        assert follow_up["result"]["isError"] is False
    finally:
        client.close()

    assert client.process.returncode == 0
    assert not (tmp_path / ".napseer").exists()


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


def test_reloads_stable_launcher_when_watched_runtime_is_replaced(tmp_path):
    runtime_path = tmp_path / "runtime.py"
    launcher_path = tmp_path / "launcher.py"
    write_worker(runtime_path, 1)
    launcher_path.write_text(
        "import runpy\n"
        f"runpy.run_path({str(runtime_path)!r}, run_name='__main__')\n",
        encoding="utf-8",
    )
    client = SupervisorClient(launcher_path, watch_paths=(runtime_path,))
    try:
        first = client.request(1, "initialize")
        launcher_identity = launcher_path.stat().st_mtime_ns

        time.sleep(0.01)
        write_worker(runtime_path, 2)
        os.utime(runtime_path, None)

        second = client.request(2, "tools/list")

        assert first["result"]["version"] == 1
        assert second["result"]["version"] == 2
        assert launcher_path.stat().st_mtime_ns == launcher_identity
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
