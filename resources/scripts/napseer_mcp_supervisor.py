#!/usr/bin/env python3
"""Stable stdio supervisor for the generated Napseer MCP worker.

Codex keeps the stdio process it launches for the lifetime of a client
session. If that process exits, Codex keeps a closed transport and does not
respawn it. This supervisor owns the client-facing pipes and treats
``napseer_mcp_server.py`` as a replaceable worker:

- a worker that exited between requests is restarted before the next request;
- a wrapper file replaced by ``nap update`` is reloaded before the next request;
- a worker that exits during a tool call is restarted, while the interrupted
  call receives a bounded JSON-RPC error and is never replayed automatically.

The supervisor never logs MCP payloads, tool results, auth state, or child
stderr. Protocol stdout contains JSON-RPC responses only.
"""

from __future__ import annotations

import json
import os
import pathlib
import selectors
import signal
import subprocess
import sys
import threading
from typing import BinaryIO


WORKER_PATH = pathlib.Path(
    os.environ.get(
        "NAPSEER_MCP_WORKER_PATH",
        pathlib.Path(__file__).resolve().with_name("napseer_mcp_server.py"),
    )
).expanduser()
RESPONSE_TIMEOUT_SECONDS = max(
    1,
    int(os.environ.get("NAPSEER_MCP_RESPONSE_TIMEOUT_SECONDS", "300")),
)
WORKER_RESTARTED_ERROR = -32098


class WorkerUnavailable(RuntimeError):
    """The replaceable MCP worker could not complete a request."""


def source_identity(path: pathlib.Path) -> tuple[int, int, int]:
    stat = path.stat()
    return stat.st_ino, stat.st_size, stat.st_mtime_ns


def request_id(message: bytes):
    try:
        payload = json.loads(message)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    return payload.get("id")


def is_notification(message: bytes) -> bool:
    try:
        payload = json.loads(message)
    except (TypeError, ValueError):
        return False
    return isinstance(payload, dict) and "id" not in payload


def worker_error(message: bytes) -> bytes:
    payload = {
        "jsonrpc": "2.0",
        "id": request_id(message),
        "error": {
            "code": WORKER_RESTARTED_ERROR,
            "message": "Napseer MCP worker restarted; retry this tool call.",
        },
    }
    return json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n"


class Worker:
    def __init__(self, path: pathlib.Path):
        self.path = path
        self.process: subprocess.Popen[bytes] | None = None
        self.identity: tuple[int, int, int] | None = None

    def _discard_stderr(self, stream: BinaryIO) -> None:
        try:
            for _line in stream:
                pass
        finally:
            stream.close()

    def start(self) -> None:
        self.stop()
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
        self.process = process
        self.identity = identity
        threading.Thread(
            target=self._discard_stderr,
            args=(process.stderr,),
            daemon=True,
        ).start()

    def stop(self) -> None:
        process = self.process
        self.process = None
        self.identity = None
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
                stream.close()

    def ensure_current(self) -> None:
        try:
            current_identity = source_identity(self.path)
        except OSError as exc:
            self.stop()
            raise WorkerUnavailable("Napseer MCP worker is missing") from exc
        if (
            self.process is None
            or self.process.poll() is not None
            or self.identity != current_identity
        ):
            self.start()

    def _read_response(self) -> bytes:
        process = self.process
        if process is None or process.stdout is None:
            raise WorkerUnavailable("Napseer MCP worker is not running")
        selector = selectors.DefaultSelector()
        try:
            selector.register(process.stdout, selectors.EVENT_READ)
            if not selector.select(RESPONSE_TIMEOUT_SECONDS):
                raise WorkerUnavailable("Napseer MCP worker response timed out")
            response = process.stdout.readline()
        finally:
            selector.close()
        if not response:
            raise WorkerUnavailable("Napseer MCP worker exited before responding")
        try:
            json.loads(response)
        except ValueError as exc:
            raise WorkerUnavailable("Napseer MCP worker returned invalid JSON") from exc
        return response if response.endswith(b"\n") else response + b"\n"

    def exchange(self, message: bytes) -> bytes | None:
        self.ensure_current()
        process = self.process
        if process is None or process.stdin is None:
            raise WorkerUnavailable("Napseer MCP worker is not running")
        try:
            process.stdin.write(message if message.endswith(b"\n") else message + b"\n")
            process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise WorkerUnavailable("Napseer MCP worker input closed") from exc
        if is_notification(message):
            return None
        return self._read_response()


def supervise() -> int:
    worker = Worker(WORKER_PATH)

    def stop_worker(_signum=None, _frame=None):
        worker.stop()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop_worker)
    signal.signal(signal.SIGINT, stop_worker)

    for message in sys.stdin.buffer:
        if not message.strip():
            continue
        try:
            response = worker.exchange(message)
        except WorkerUnavailable:
            worker.stop()
            try:
                worker.start()
            except WorkerUnavailable:
                pass
            response = None if is_notification(message) else worker_error(message)
        if response is not None:
            sys.stdout.buffer.write(response)
            sys.stdout.buffer.flush()

    worker.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(supervise())
