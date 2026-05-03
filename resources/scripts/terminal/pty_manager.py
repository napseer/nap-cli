"""Stdlib PTY session manager for the gateway terminal vertical slice."""

from __future__ import annotations

import errno
import fcntl
import os
import select
import shlex
import signal
import struct
import subprocess
import termios
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from typing import Callable, Deque, Iterable


OutputCallback = Callable[[str, int, bytes], None]
UnsubscribeCallback = Callable[[], None]


@dataclass(frozen=True)
class OutputChunk:
    seq: int
    data: bytes
    ts: float


@dataclass(frozen=True)
class AttachResult:
    terminal_id: str
    chunks: tuple[OutputChunk, ...]
    replay_gap: bool
    requested_after_seq: int | None
    oldest_seq: int | None
    newest_seq: int


@dataclass(frozen=True)
class TerminalInfo:
    terminal_id: str
    pid: int
    command: tuple[str, ...]
    cwd: str
    rows: int
    cols: int
    output_seq: int
    oldest_output_seq: int | None
    created_at: float
    last_activity: float
    closed: bool
    exit_code: int | None


class TerminalSession:
    def __init__(
        self,
        *,
        terminal_id: str,
        process: subprocess.Popen[bytes],
        master_fd: int,
        command: tuple[str, ...],
        cwd: str,
        rows: int,
        cols: int,
        ring_chunks: int,
        output_callback: OutputCallback | None,
    ) -> None:
        self.terminal_id = terminal_id
        self.process = process
        self.master_fd = master_fd
        self.command = command
        self.cwd = cwd
        self.rows = rows
        self.cols = cols
        self.created_at = time.time()
        self.last_activity = self.created_at
        self.output_seq = 0
        self.last_input_seq: int | None = None
        self.closed = False
        self.exit_code: int | None = None
        self._ring: Deque[OutputChunk] = deque(maxlen=ring_chunks)
        self._callbacks: dict[str, OutputCallback] = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._output_callback = output_callback
        self._reader = threading.Thread(
            target=self._read_loop,
            name=f"napseer-terminal-{terminal_id}",
            daemon=True,
        )
        self._reader.start()

    def write(self, data: bytes, input_seq: int | None = None) -> bool:
        if not isinstance(data, bytes):
            raise TypeError("terminal input data must be bytes")
        if not data:
            return False
        with self._lock:
            self._ensure_open()
            if input_seq is not None:
                if self.last_input_seq is not None and input_seq <= self.last_input_seq:
                    return False
                self.last_input_seq = input_seq
            self.last_activity = time.time()
            fd = self.master_fd
        self._write_all(fd, data)
        return True

    def resize(self, rows: int, cols: int) -> None:
        rows, cols = _normalize_size(rows, cols)
        with self._lock:
            self._ensure_open()
            self.rows = rows
            self.cols = cols
            self.last_activity = time.time()
            fd = self.master_fd
        _set_winsize(fd, rows, cols)
        try:
            os.killpg(os.getpgid(self.process.pid), signal.SIGWINCH)
        except ProcessLookupError:
            pass

    def attach(self, last_output_seq: int | None = None) -> AttachResult:
        with self._lock:
            chunks = tuple(self._ring)
            newest_seq = self.output_seq
            oldest_seq = chunks[0].seq if chunks else None
            replay_gap = False
            if last_output_seq is None:
                selected = chunks
            elif oldest_seq is not None and last_output_seq < oldest_seq - 1:
                selected = chunks
                replay_gap = True
            else:
                selected = tuple(chunk for chunk in chunks if chunk.seq > last_output_seq)
            self.last_activity = time.time()
        return AttachResult(
            terminal_id=self.terminal_id,
            chunks=selected,
            replay_gap=replay_gap,
            requested_after_seq=last_output_seq,
            oldest_seq=oldest_seq,
            newest_seq=newest_seq,
        )

    def close(self) -> None:
        process: subprocess.Popen[bytes]
        fd: int
        with self._lock:
            if self.closed:
                return
            self.closed = True
            self.last_activity = time.time()
            process = self.process
            fd = self.master_fd
            self._stop.set()
        try:
            os.close(fd)
        except OSError:
            pass
        if process.poll() is None:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=2.0)
        with self._lock:
            self.exit_code = process.poll()

    def info(self) -> TerminalInfo:
        with self._lock:
            oldest_seq = self._ring[0].seq if self._ring else None
            exit_code = self.process.poll()
            if exit_code is not None:
                self.exit_code = exit_code
            return TerminalInfo(
                terminal_id=self.terminal_id,
                pid=self.process.pid,
                command=self.command,
                cwd=self.cwd,
                rows=self.rows,
                cols=self.cols,
                output_seq=self.output_seq,
                oldest_output_seq=oldest_seq,
                created_at=self.created_at,
                last_activity=self.last_activity,
                closed=self.closed,
                exit_code=self.exit_code,
            )

    def _read_loop(self) -> None:
        while not self._stop.is_set():
            try:
                ready, _, _ = select.select([self.master_fd], [], [], 0.05)
            except (OSError, ValueError):
                break
            if not ready:
                if self.process.poll() is not None:
                    break
                continue
            try:
                data = os.read(self.master_fd, 8192)
            except OSError as exc:
                if exc.errno in (errno.EIO, errno.EBADF):
                    break
                continue
            if not data:
                break
            with self._lock:
                self.output_seq += 1
                seq = self.output_seq
                chunk = OutputChunk(seq=seq, data=data, ts=time.time())
                self._ring.append(chunk)
                self.last_activity = chunk.ts
                callback = self._output_callback
                callbacks = tuple(self._callbacks.values())
            if callback is not None:
                callback(self.terminal_id, seq, data)
            for listener in callbacks:
                listener(self.terminal_id, seq, data)
        with self._lock:
            self.exit_code = self.process.poll()
            self.closed = True
            self._stop.set()

    def _ensure_open(self) -> None:
        if self.closed or self.process.poll() is not None:
            self.closed = True
            self.exit_code = self.process.poll()
            raise RuntimeError(f"terminal {self.terminal_id} is closed")

    def subscribe(self, callback: OutputCallback) -> UnsubscribeCallback:
        subscription_id = uuid.uuid4().hex
        with self._lock:
            self._callbacks[subscription_id] = callback

        def unsubscribe() -> None:
            with self._lock:
                self._callbacks.pop(subscription_id, None)

        return unsubscribe

    @staticmethod
    def _write_all(fd: int, data: bytes) -> None:
        view = memoryview(data)
        while view:
            try:
                written = os.write(fd, view)
            except BlockingIOError:
                select.select([], [fd], [], 1.0)
                continue
            view = view[written:]


class PtySessionManager:
    def __init__(
        self,
        *,
        ring_chunks: int = 512,
        output_callback: OutputCallback | None = None,
    ) -> None:
        if ring_chunks < 1:
            raise ValueError("ring_chunks must be positive")
        self._ring_chunks = ring_chunks
        self._output_callback = output_callback
        self._sessions: dict[str, TerminalSession] = {}
        self._lock = threading.RLock()

    def open(
        self,
        command: str | Iterable[str] | None = None,
        cwd: str | None = None,
        rows: int = 24,
        cols: int = 80,
    ) -> TerminalInfo:
        rows, cols = _normalize_size(rows, cols)
        command_tuple = _normalize_command(command)
        cwd_path = os.path.abspath(cwd or os.getcwd())
        if not os.path.isdir(cwd_path):
            raise FileNotFoundError(f"terminal cwd does not exist: {cwd_path}")

        master_fd, slave_fd = os.openpty()
        try:
            _set_winsize(slave_fd, rows, cols)
            process = subprocess.Popen(
                command_tuple,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                cwd=cwd_path,
                close_fds=True,
                start_new_session=True,
            )
        finally:
            os.close(slave_fd)

        _set_nonblocking(master_fd)
        terminal_id = f"term_{uuid.uuid4().hex}"
        session = TerminalSession(
            terminal_id=terminal_id,
            process=process,
            master_fd=master_fd,
            command=command_tuple,
            cwd=cwd_path,
            rows=rows,
            cols=cols,
            ring_chunks=self._ring_chunks,
            output_callback=self._output_callback,
        )
        with self._lock:
            self._sessions[terminal_id] = session
        return session.info()

    def write(self, terminal_id: str, data: bytes, input_seq: int | None = None) -> bool:
        return self._get(terminal_id).write(data, input_seq=input_seq)

    def resize(self, terminal_id: str, rows: int, cols: int) -> TerminalInfo:
        session = self._get(terminal_id)
        session.resize(rows, cols)
        return session.info()

    def attach(
        self,
        terminal_id: str,
        last_output_seq: int | None = None,
    ) -> AttachResult:
        return self._get(terminal_id).attach(last_output_seq=last_output_seq)

    def subscribe(self, terminal_id: str, callback: OutputCallback) -> UnsubscribeCallback:
        return self._get(terminal_id).subscribe(callback)

    def close(self, terminal_id: str) -> None:
        session = self._get(terminal_id)
        session.close()
        with self._lock:
            self._sessions.pop(terminal_id, None)

    def list(self) -> list[TerminalInfo]:
        with self._lock:
            sessions = tuple(self._sessions.values())
        return [session.info() for session in sessions]

    def _get(self, terminal_id: str) -> TerminalSession:
        with self._lock:
            session = self._sessions.get(terminal_id)
        if session is None:
            raise KeyError(f"unknown terminal: {terminal_id}")
        return session


def _normalize_command(command: str | Iterable[str] | None) -> tuple[str, ...]:
    if command is None:
        shell = os.environ.get("SHELL") or "/bin/sh"
        return (shell,)
    if isinstance(command, str):
        return tuple(shlex.split(command))
    command_tuple = tuple(command)
    if not command_tuple:
        raise ValueError("terminal command cannot be empty")
    return command_tuple


def _normalize_size(rows: int, cols: int) -> tuple[int, int]:
    rows = int(rows)
    cols = int(cols)
    if rows < 1 or cols < 1:
        raise ValueError("terminal rows and cols must be positive")
    return rows, cols


def _set_winsize(fd: int, rows: int, cols: int) -> None:
    winsize = struct.pack("HHHH", rows, cols, 0, 0)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)


def _set_nonblocking(fd: int) -> None:
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)


default_manager = PtySessionManager()


def open(
    command: str | Iterable[str] | None = None,
    cwd: str | None = None,
    rows: int = 24,
    cols: int = 80,
) -> TerminalInfo:
    return default_manager.open(command=command, cwd=cwd, rows=rows, cols=cols)


def write(terminal_id: str, data: bytes, input_seq: int | None = None) -> bool:
    return default_manager.write(terminal_id, data, input_seq=input_seq)


def resize(terminal_id: str, rows: int, cols: int) -> TerminalInfo:
    return default_manager.resize(terminal_id, rows, cols)


def attach(terminal_id: str, last_output_seq: int | None = None) -> AttachResult:
    return default_manager.attach(terminal_id, last_output_seq=last_output_seq)


def subscribe(terminal_id: str, callback: OutputCallback) -> UnsubscribeCallback:
    return default_manager.subscribe(terminal_id, callback)


def close(terminal_id: str) -> None:
    default_manager.close(terminal_id)


def list() -> list[TerminalInfo]:
    return default_manager.list()
