#!/usr/bin/env python3
"""Stable, multiplexed stdio supervisor for the Napseer MCP worker.

Codex keeps the stdio process it launches for the lifetime of a client
session. This supervisor owns those client-facing pipes while treating
``napseer_mcp_server.py`` as a replaceable worker:

- valid requests are forwarded without head-of-line blocking and responses
  are correlated by JSON-RPC request id;
- notifications never create client-facing responses;
- cancellation releases the matching pending request and forwards the
  notification to the worker;
- a worker exit fails every affected in-flight request once and never replays
  it automatically;
- a replaced worker is reloaded at the next quiescent request boundary.

The supervisor never logs MCP payloads, tool results, auth state, or child
stderr. Protocol stdout contains JSON-RPC messages only.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import pathlib
import queue
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from typing import BinaryIO, Callable


WORKER_PATH = pathlib.Path(
    os.environ.get(
        "NAPSEER_MCP_WORKER_PATH",
        pathlib.Path(__file__).resolve().with_name("napseer_mcp_server.py"),
    )
).expanduser()
RESPONSE_TIMEOUT_SECONDS = max(
    1,
    int(os.environ.get("NAPSEER_MCP_RESPONSE_TIMEOUT_SECONDS", "60")),
)
MAX_IN_FLIGHT_REQUESTS = max(
    2,
    int(os.environ.get("NAPSEER_MCP_MAX_IN_FLIGHT_REQUESTS", "32")),
)
WORKER_RESTARTED_ERROR = -32098
_CANCELLED = object()


class WorkerUnavailable(RuntimeError):
    """The replaceable MCP worker could not complete a request."""


def source_identity(path: pathlib.Path) -> tuple[int, int, int]:
    stat = path.stat()
    return stat.st_ino, stat.st_size, stat.st_mtime_ns


def parse_message(message: bytes):
    try:
        payload = json.loads(message)
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def request_id(message: bytes):
    payload = parse_message(message)
    return payload.get("id") if payload is not None else None


def request_key(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def request_method(message: bytes) -> str:
    payload = parse_message(message)
    return str(payload.get("method") or "") if payload is not None else ""


def is_notification(message: bytes) -> bool:
    payload = parse_message(message)
    return payload is not None and "id" not in payload


def cancelled_request_id(message: bytes):
    payload = parse_message(message)
    if payload is None or payload.get("method") != "notifications/cancelled":
        return False, None
    params = payload.get("params") or {}
    if not isinstance(params, dict) or "requestId" not in params:
        return False, None
    return True, params.get("requestId")


def worker_error(message: bytes) -> bytes:
    payload = {
        "jsonrpc": "2.0",
        "id": request_id(message),
        "error": {
            "code": WORKER_RESTARTED_ERROR,
            "message": (
                "Napseer MCP could not complete this request. The outcome may be "
                "uncertain; inspect state before retrying a mutation."
            ),
        },
    }
    return json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n"


@dataclass
class PendingRequest:
    message: bytes
    key: str
    generation: int
    response: queue.Queue = field(default_factory=lambda: queue.Queue(maxsize=1))


class Worker:
    def __init__(self, path: pathlib.Path, unsolicited: Callable[[bytes], None] | None = None):
        self.path = path
        self.process: subprocess.Popen[bytes] | None = None
        self.identity: tuple[int, int, int] | None = None
        self.generation = 0
        self.unsolicited = unsolicited
        self._lifecycle_lock = threading.RLock()
        self._stdin_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending: dict[str, PendingRequest] = {}
        self._reload_pending = False

    def _discard_stderr(self, stream: BinaryIO) -> None:
        try:
            for _line in stream:
                pass
        finally:
            stream.close()

    def _pending_count(self, generation: int | None = None) -> int:
        with self._pending_lock:
            if generation is None:
                return len(self._pending)
            return sum(1 for item in self._pending.values() if item.generation == generation)

    def _signal(self, pending: PendingRequest, value) -> None:
        try:
            pending.response.put_nowait(value)
        except queue.Full:
            pass

    def _fail_generation(self, generation: int, error: WorkerUnavailable) -> None:
        failed = []
        with self._pending_lock:
            for key, pending in list(self._pending.items()):
                if pending.generation != generation:
                    continue
                self._pending.pop(key, None)
                failed.append(pending)
        for pending in failed:
            self._signal(pending, error)

    def _deliver_response(self, generation: int, response: bytes) -> bool:
        payload = parse_message(response)
        if payload is None:
            return False
        normalized = response if response.endswith(b"\n") else response + b"\n"
        if "id" in payload:
            key = request_key(payload.get("id"))
            with self._pending_lock:
                pending = self._pending.get(key)
                if pending is not None and pending.generation == generation:
                    self._pending.pop(key, None)
                else:
                    pending = None
            if pending is not None:
                self._signal(pending, normalized)
                return True
        if "method" in payload and self.unsolicited is not None:
            self.unsolicited(normalized)
        return True

    def _read_stdout(self, process: subprocess.Popen[bytes], generation: int) -> None:
        stream = process.stdout
        if stream is None:
            self._fail_generation(generation, WorkerUnavailable("Napseer MCP worker output is unavailable"))
            return
        invalid_response = False
        try:
            for response in stream:
                if not self._deliver_response(generation, response):
                    invalid_response = True
                    break
        except (OSError, ValueError):
            pass
        if invalid_response and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass
        self._fail_generation(
            generation,
            WorkerUnavailable(
                "Napseer MCP worker returned invalid JSON"
                if invalid_response
                else "Napseer MCP worker exited before responding"
            ),
        )

    def _stop_locked(self) -> None:
        process = self.process
        generation = self.generation
        self.process = None
        self.identity = None
        self._reload_pending = False
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        self._fail_generation(generation, WorkerUnavailable("Napseer MCP worker stopped"))

    def start(self) -> None:
        with self._lifecycle_lock:
            self._stop_locked()
            try:
                identity = source_identity(self.path)
                process = subprocess.Popen(
                    [sys.executable, "-u", str(self.path)],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    bufsize=0,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise WorkerUnavailable("unable to start Napseer MCP worker") from exc
            if process.stdin is None or process.stdout is None or process.stderr is None:
                process.kill()
                process.wait()
                raise WorkerUnavailable("Napseer MCP worker pipes are unavailable")
            self.generation += 1
            generation = self.generation
            self.process = process
            self.identity = identity
            threading.Thread(
                target=self._discard_stderr,
                args=(process.stderr,),
                name=f"napseer-mcp-stderr-{generation}",
                daemon=True,
            ).start()
            threading.Thread(
                target=self._read_stdout,
                args=(process, generation),
                name=f"napseer-mcp-stdout-{generation}",
                daemon=True,
            ).start()

    def stop(self) -> None:
        with self._lifecycle_lock:
            self._stop_locked()

    def ensure_current(self) -> None:
        with self._lifecycle_lock:
            try:
                current_identity = source_identity(self.path)
            except OSError as exc:
                self._stop_locked()
                raise WorkerUnavailable("Napseer MCP worker is missing") from exc
            process_dead = self.process is None or self.process.poll() is not None
            source_changed = self.identity is not None and self.identity != current_identity
            if source_changed and not process_dead and self._pending_count(self.generation):
                self._reload_pending = True
                return
            if process_dead or self.identity is None or source_changed or self._reload_pending:
                self.start()

    def _write(self, process: subprocess.Popen[bytes], message: bytes) -> None:
        with self._stdin_lock:
            if self.process is not process or process.poll() is not None or process.stdin is None:
                raise WorkerUnavailable("Napseer MCP worker is not running")
            try:
                process.stdin.write(message if message.endswith(b"\n") else message + b"\n")
                process.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise WorkerUnavailable("Napseer MCP worker input closed") from exc

    def submit(self, message: bytes) -> PendingRequest | None:
        self.ensure_current()
        with self._lifecycle_lock:
            process = self.process
            generation = self.generation
        if process is None:
            raise WorkerUnavailable("Napseer MCP worker is not running")
        if is_notification(message):
            self._write(process, message)
            return None

        key = request_key(request_id(message))
        pending = PendingRequest(message=message, key=key, generation=generation)
        with self._pending_lock:
            if key in self._pending:
                raise WorkerUnavailable("duplicate in-flight JSON-RPC request id")
            if len(self._pending) >= MAX_IN_FLIGHT_REQUESTS:
                raise WorkerUnavailable("too many in-flight Napseer MCP requests")
            self._pending[key] = pending
        try:
            self._write(process, message)
        except WorkerUnavailable:
            with self._pending_lock:
                if self._pending.get(key) is pending:
                    self._pending.pop(key, None)
            raise
        return pending

    def cancel(self, value) -> bool:
        key = request_key(value)
        with self._pending_lock:
            pending = self._pending.get(key)
            if pending is None or request_method(pending.message) == "initialize":
                return False
            self._pending.pop(key, None)
        self._signal(pending, _CANCELLED)
        return True

    def wait(self, pending: PendingRequest) -> bytes | None:
        try:
            result = pending.response.get(timeout=RESPONSE_TIMEOUT_SECONDS)
        except queue.Empty as exc:
            with self._pending_lock:
                if self._pending.get(pending.key) is pending:
                    self._pending.pop(pending.key, None)
            raise WorkerUnavailable("Napseer MCP worker response timed out") from exc
        if result is _CANCELLED:
            return None
        if isinstance(result, WorkerUnavailable):
            raise result
        return result


def supervise() -> int:
    output_lock = threading.Lock()
    closing = threading.Event()

    def emit(response: bytes) -> None:
        if closing.is_set():
            return
        with output_lock:
            sys.stdout.buffer.write(response if response.endswith(b"\n") else response + b"\n")
            sys.stdout.buffer.flush()

    worker = Worker(WORKER_PATH, unsolicited=emit)
    waiters = concurrent.futures.ThreadPoolExecutor(
        max_workers=MAX_IN_FLIGHT_REQUESTS,
        thread_name_prefix="napseer-mcp-request",
    )

    def finish(message: bytes, pending: PendingRequest) -> None:
        try:
            response = worker.wait(pending)
        except WorkerUnavailable:
            response = worker_error(message)
        if response is not None:
            emit(response)

    def stop_worker(_signum=None, _frame=None):
        closing.set()
        worker.stop()
        waiters.shutdown(wait=False, cancel_futures=True)
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop_worker)
    signal.signal(signal.SIGINT, stop_worker)

    try:
        for message in sys.stdin.buffer:
            if not message.strip():
                continue
            cancelled, value = cancelled_request_id(message)
            if cancelled:
                worker.cancel(value)
                try:
                    worker.submit(message)
                except WorkerUnavailable:
                    pass
                continue
            try:
                pending = worker.submit(message)
            except WorkerUnavailable:
                if not is_notification(message):
                    emit(worker_error(message))
                continue
            if pending is not None:
                waiters.submit(finish, message, pending)
    finally:
        closing.set()
        worker.stop()
        waiters.shutdown(wait=True, cancel_futures=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(supervise())
