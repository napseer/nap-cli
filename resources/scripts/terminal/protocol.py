"""Terminal protocol constants and small frame helpers.

The relay adapter will encrypt these payloads before sending them over the
gateway relay. Keep this module content-only: no terminal output logging and no
network side effects.
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone
from typing import Any

TERMINAL_OPEN = "terminal.open"
TERMINAL_OPENED = "terminal.opened"
TERMINAL_DATA = "terminal.data"
TERMINAL_OUTPUT = "terminal.output"
TERMINAL_RESIZE = "terminal.resize"
TERMINAL_DETACH = "terminal.detach"
TERMINAL_CLOSE = "terminal.close"
TERMINAL_REPLAY_GAP = "terminal.replay_gap"
TERMINAL_ERROR = "terminal.error"


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def encode_bytes(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def decode_bytes(value: str) -> bytes:
    return base64.b64decode(value.encode("ascii"), validate=True)


def output_frame(terminal_id: str, seq: int, data: bytes) -> dict[str, Any]:
    return {
        "type": TERMINAL_OUTPUT,
        "terminal_id": terminal_id,
        "seq": seq,
        "data_b64": encode_bytes(data),
        "gateway_output_sent_at": utc_timestamp(),
    }


def opened_frame(
    terminal_id: str,
    *,
    rows: int,
    cols: int,
    output_seq: int = 0,
    attached: bool = True,
    pid: int | None = None,
) -> dict[str, Any]:
    frame: dict[str, Any] = {
        "type": TERMINAL_OPENED,
        "terminal_id": terminal_id,
        "rows": rows,
        "cols": cols,
        "output_seq": output_seq,
        "attached": attached,
    }
    if pid is not None:
        frame["pid"] = pid
    return frame


def replay_gap_frame(
    terminal_id: str,
    *,
    requested_after_seq: int | None,
    oldest_seq: int | None,
    newest_seq: int,
) -> dict[str, Any]:
    return {
        "type": TERMINAL_REPLAY_GAP,
        "terminal_id": terminal_id,
        "requested_after_seq": requested_after_seq,
        "oldest_seq": oldest_seq,
        "newest_seq": newest_seq,
    }


def error_frame(
    terminal_id: str | None,
    code: str,
    message: str,
) -> dict[str, Any]:
    frame: dict[str, Any] = {
        "type": TERMINAL_ERROR,
        "code": code,
        "message": message,
    }
    if terminal_id:
        frame["terminal_id"] = terminal_id
    return frame
