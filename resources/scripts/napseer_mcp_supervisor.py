#!/usr/bin/env python3
"""Stable, multiplexed stdio supervisor for the Napseer MCP worker.

Codex keeps the stdio process it launches for the lifetime of a client
session. This supervisor owns those client-facing pipes while treating
``napseer_mcp_server.py`` as a replaceable worker:

- valid requests are forwarded without head-of-line blocking and responses
  are correlated by JSON-RPC request id;
- notifications never create client-facing responses;
- cancellation stops waiting for a result but retains unfinished work until
  the worker acknowledges cleanup, with bounded grace before termination;
- a worker exit fails every affected in-flight request once and never replays
  it automatically;
- a replaced worker, or an explicitly configured runtime dependency behind a
  stable launcher, is reloaded at the next quiescent request boundary.

The supervisor never logs MCP payloads, tool results, auth state, or child
stderr. Protocol stdout contains JSON-RPC messages only.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import pathlib
import queue
import select
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import BinaryIO, Callable


WORKER_PATH = pathlib.Path(
    os.environ.get(
        "NAPSEER_MCP_WORKER_PATH",
        pathlib.Path(__file__).resolve().with_name("napseer_mcp_server.py"),
    )
).expanduser()
WORKER_WATCH_PATHS = tuple(
    pathlib.Path(value).expanduser()
    for value in os.environ.get("NAPSEER_MCP_WORKER_WATCH_PATHS", "").split(os.pathsep)
    if value.strip()
)
RESPONSE_TIMEOUT_SECONDS = max(
    1,
    int(os.environ.get("NAPSEER_MCP_RESPONSE_TIMEOUT_SECONDS", "60")),
)
MAX_IN_FLIGHT_REQUESTS = max(
    2,
    int(os.environ.get("NAPSEER_MCP_MAX_IN_FLIGHT_REQUESTS", "32")),
)
WORKER_RESTARTED_ERROR = -32098
REQUEST_REJECTED_ERROR = -32097
CANCEL_GRACE_SECONDS = max(1, int(os.environ.get("NAPSEER_MCP_CANCEL_GRACE_SECONDS", "10")))
WRITE_TIMEOUT_SECONDS = max(1, int(os.environ.get("NAPSEER_MCP_WRITE_TIMEOUT_SECONDS", "5")))
REQUEST_FINISHED = "notifications/napseer/requestFinished"
_CANCELLED = object()


class WorkerUnavailable(RuntimeError):
    """The replaceable MCP worker could not complete a request."""


class RequestRejected(WorkerUnavailable):
    """The request was not sent to the worker and cannot have executed."""


def source_identity(path: pathlib.Path) -> tuple[int, int, int]:
    stat = path.stat()
    return stat.st_ino, stat.st_size, stat.st_mtime_ns


def runtime_identity(
    worker_path: pathlib.Path,
    watch_paths: tuple[pathlib.Path, ...] = (),
) -> tuple[tuple[int, int, int], ...]:
    """Identify every source whose replacement changes the worker runtime."""

    return tuple(source_identity(path) for path in (worker_path, *watch_paths))


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


def worker_error(message: bytes, *, rejected=False) -> bytes:
    payload = {
        "jsonrpc": "2.0",
        "id": request_id(message),
        "error": {
            "code": REQUEST_REJECTED_ERROR if rejected else WORKER_RESTARTED_ERROR,
            "message": (
                "Napseer MCP could not complete this request. The outcome may be "
                "uncertain; inspect state before retrying a mutation."
            ) if not rejected else "Napseer MCP is busy or this request id is still active. This request was not started; retry later.",
        },
    }
    return json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n"


@dataclass
class PendingRequest:
    message: bytes
    key: str
    generation: int
    response: queue.Queue = field(default_factory=lambda: queue.Queue(maxsize=1))
    cancelled: bool = False
    cancel_timer: threading.Timer | None = None


class Worker:
    def __init__(
        self,
        path: pathlib.Path,
        unsolicited: Callable[[bytes], None] | None = None,
        watch_paths: tuple[pathlib.Path, ...] = (),
    ):
        self.path = path
        self.watch_paths = tuple(watch_paths)
        self.process: subprocess.Popen[bytes] | None = None
        self.identity: tuple[tuple[int, int, int], ...] | None = None
        self.generation = 0
        self.unsolicited = unsolicited
        self._lifecycle_lock = threading.RLock()
        self._stdin_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending: dict[str, PendingRequest] = {}
        self._reload_pending = False

    def _discard_stderr(self, stream: BinaryIO) -> None:
        try:
            while stream.read(4096):
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
                if pending.cancel_timer is not None:
                    pending.cancel_timer.cancel()
                failed.append(pending)
        for pending in failed:
            self._signal(pending, error)

    def _deliver_response(self, generation: int, response: bytes) -> bool:
        payload = parse_message(response)
        if payload is None:
            return False
        normalized = response if response.endswith(b"\n") else response + b"\n"
        if payload.get("method") == REQUEST_FINISHED:
            params = payload.get("params") or {}
            if isinstance(params, dict) and "requestId" in params:
                key = request_key(params["requestId"])
                with self._pending_lock:
                    pending = self._pending.get(key)
                    if pending is not None and pending.generation == generation and pending.cancelled:
                        self._pending.pop(key, None)
                        if pending.cancel_timer is not None:
                            pending.cancel_timer.cancel()
            return True
        if "id" in payload:
            key = request_key(payload.get("id"))
            with self._pending_lock:
                pending = self._pending.get(key)
                if pending is not None and pending.generation == generation:
                    self._pending.pop(key, None)
                    if pending.cancel_timer is not None:
                        pending.cancel_timer.cancel()
                else:
                    pending = None
            if pending is not None:
                if not pending.cancelled:
                    self._signal(pending, normalized)
                return True
        if generation == self.generation and "method" in payload and self.unsolicited is not None:
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
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
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
                identity = runtime_identity(self.path, self.watch_paths)
                process = subprocess.Popen(
                    [sys.executable, "-u", str(self.path)],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    bufsize=0,
                    env={**os.environ, "NAPSEER_MCP_SUPERVISED": "1"},
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
            os.set_blocking(process.stdin.fileno(), False)
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
                current_identity = runtime_identity(self.path, self.watch_paths)
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
        deadline = time.monotonic() + WRITE_TIMEOUT_SECONDS
        if not self._stdin_lock.acquire(timeout=WRITE_TIMEOUT_SECONDS):
            raise WorkerUnavailable("Napseer MCP worker input timed out")
        try:
            if self.process is not process or process.poll() is not None or process.stdin is None:
                raise WorkerUnavailable("Napseer MCP worker is not running")
            try:
                remaining = memoryview(message if message.endswith(b"\n") else message + b"\n")
                while remaining:
                    timeout = deadline - time.monotonic()
                    if timeout <= 0 or self.process is not process:
                        raise WorkerUnavailable("Napseer MCP worker input timed out")
                    fd = process.stdin.fileno()
                    if not select.select([], [fd], [], timeout)[1]:
                        raise WorkerUnavailable("Napseer MCP worker input timed out")
                    try:
                        count = os.write(fd, remaining)
                        remaining = remaining[count:]
                    except BlockingIOError:
                        continue
            except (BrokenPipeError, OSError, ValueError) as exc:
                raise WorkerUnavailable("Napseer MCP worker input closed") from exc
        finally:
            self._stdin_lock.release()

    def submit(self, message: bytes) -> PendingRequest | None:
        self.ensure_current()
        with self._lifecycle_lock:
            process = self.process
            generation = self.generation
        if process is None:
            raise WorkerUnavailable("Napseer MCP worker is not running")
        if is_notification(message):
            try:
                self._write(process, message)
            except WorkerUnavailable:
                self.stop()
                raise
            return None

        key = request_key(request_id(message))
        pending = PendingRequest(message=message, key=key, generation=generation)
        with self._pending_lock:
            if key in self._pending:
                raise RequestRejected("duplicate in-flight JSON-RPC request id")
            if len(self._pending) >= MAX_IN_FLIGHT_REQUESTS:
                raise RequestRejected("too many unfinished Napseer MCP requests")
            self._pending[key] = pending
        try:
            self._write(process, message)
        except WorkerUnavailable:
            with self._pending_lock:
                if self._pending.get(key) is pending:
                    self._pending.pop(key, None)
            # A partial write has an uncertain outcome and corrupts framing.
            # Stop this generation rather than appending another request to it.
            self.stop()
            raise
        return pending

    def cancel(self, value, *, expected: PendingRequest | None = None) -> bool:
        key = request_key(value)
        with self._pending_lock:
            pending = self._pending.get(key)
            if expected is not None and pending is not expected:
                return False
            if pending is None or request_method(pending.message) == "initialize":
                return False
            if pending.cancelled:
                return True
            pending.cancelled = True
            pending.cancel_timer = threading.Timer(CANCEL_GRACE_SECONDS, self._expire_cancelled, args=(pending,))
            pending.cancel_timer.daemon = True
            timer = pending.cancel_timer
        self._signal(pending, _CANCELLED)
        timer.start()
        process = self.process
        if process is not None and pending.generation == self.generation:
            try:
                self._write(process, json.dumps({"jsonrpc": "2.0", "method": "notifications/cancelled",
                                                "params": {"requestId": value}}).encode("utf-8"))
            except WorkerUnavailable:
                self._expire_cancelled(pending)
        return True

    def _expire_cancelled(self, pending: PendingRequest) -> None:
        with self._lifecycle_lock:
            with self._pending_lock:
                unfinished = self._pending.get(pending.key) is pending
            if unfinished and pending.generation == self.generation:
                self._stop_locked()

    def wait(self, pending: PendingRequest) -> bytes | None:
        try:
            result = pending.response.get(timeout=RESPONSE_TIMEOUT_SECONDS)
        except queue.Empty as exc:
            if request_method(pending.message) == "initialize":
                # Initialize is not cancellable, but a stuck initialization
                # must not permanently consume the worker generation.
                self._expire_cancelled(pending)
            else:
                self.cancel(request_id(pending.message), expected=pending)
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

    worker = Worker(
        WORKER_PATH,
        unsolicited=emit,
        watch_paths=WORKER_WATCH_PATHS,
    )
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
                continue
            try:
                pending = worker.submit(message)
            except WorkerUnavailable as exc:
                if not is_notification(message):
                    emit(worker_error(message, rejected=isinstance(exc, RequestRejected)))
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
