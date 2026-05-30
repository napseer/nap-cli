"""Telemetry and logging for Napseer MCP server."""

import hashlib
import json
import os
import pathlib
import platform
import sys
import threading
import time
import uuid


# Telemetry state
_telemetry_enabled = None
_telemetry_install_id = None
_telemetry_state_path = None


def local_state_dir():
    """Get the local state directory."""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~\\AppData\\Local")
        return pathlib.Path(base) / "napseer"
    return pathlib.Path.home() / ".napseer"


def state_dir_status_payload():
    """Get state directory status information."""
    state_dir = local_state_dir()
    auth_path = state_dir / "auth.json"
    
    active_project = None
    if auth_path.exists():
        try:
            auth_data = json.loads(auth_path.read_text())
            active_project = auth_data.get("project_id")
        except (json.JSONDecodeError, IOError):
            pass
    
    return {
        "state_dir": str(state_dir),
        "active_auth_path": str(auth_path),
        "active_project_id": active_project,
        "state_dir_exists": state_dir.exists(),
        "auth_configured": auth_path.exists(),
    }


def telemetry_state():
    """Get or initialize telemetry state."""
    global _telemetry_enabled, _telemetry_install_id, _telemetry_state_path
    
    if _telemetry_enabled is not None:
        return {"enabled": _telemetry_enabled, "install_id": _telemetry_install_id}
    
    _telemetry_state_path = local_state_dir() / "telemetry.json"
    
    if _telemetry_state_path.exists():
        try:
            state = json.loads(_telemetry_state_path.read_text())
            _telemetry_enabled = state.get("enabled", True)
            _telemetry_install_id = state.get("install_id")
        except (json.JSONDecodeError, IOError):
            _telemetry_enabled = True
            _telemetry_install_id = None
    else:
        _telemetry_enabled = True
        _telemetry_install_id = None
    
    return {"enabled": _telemetry_enabled, "install_id": _telemetry_install_id}


def telemetry_enabled():
    """Check if telemetry is enabled."""
    return telemetry_state()["enabled"]


def telemetry_install_id():
    """Get or generate telemetry install ID."""
    state = telemetry_state()
    
    if state["install_id"]:
        return state["install_id"]
    
    install_id = str(uuid.uuid4())
    _telemetry_state_path = local_state_dir() / "telemetry.json"
    
    try:
        _telemetry_state_path.parent.mkdir(parents=True, exist_ok=True)
        _telemetry_state_path.write_text(json.dumps({
            "enabled": True,
            "install_id": install_id,
        }))
        global _telemetry_install_id
        _telemetry_install_id = install_id
    except IOError:
        pass
    
    return install_id


def telemetry_safe_text(value, max_len=128):
    """Sanitize value for telemetry."""
    if value is None:
        return None
    text = str(value)
    if len(text) > max_len:
        text = text[:max_len] + "..."
    return text.replace("\n", " ").replace("\r", "")


def telemetry_error_code(exc):
    """Extract error code from exception."""
    exc_type = type(exc).__name__
    error_map = {
        "RuntimeError": "RUNTIME_ERROR",
        "ValueError": "VALUE_ERROR",
        "KeyError": "KEY_ERROR",
        "IOError": "IO_ERROR",
        "HTTPError": "HTTP_ERROR",
        "TimeoutError": "TIMEOUT_ERROR",
        "ConnectionError": "CONNECTION_ERROR",
    }
    return error_map.get(exc_type, "UNKNOWN_ERROR")


def send_telemetry_event(event, component, outcome="success", **fields):
    """Send a telemetry event (synchronous)."""
    if not telemetry_enabled():
        return
    
    payload = {
        "event": event,
        "component": component,
        "outcome": outcome,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "platform": platform.system(),
        "platform_version": platform.release(),
        "python_version": platform.python_version(),
        "install_id": telemetry_install_id(),
        **fields,
    }
    
    try:
        import urllib.error
        import urllib.parse
        import urllib.request
        
        base_url = os.environ.get("NAPSEER_BASE_URL", "https://api.napseer.com")
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{base_url}/v1/telemetry",
            data=data,
            headers={"Content-Type": "application/json", "User-Agent": "napseer-mcp/0.1"},
            method="POST",
        )
        urllib.request.urlopen(request, timeout=5)
    except Exception:
        pass  # Telemetry failures are silent


async def send_telemetry_event_async(event, component, outcome="success", **fields):
    """Send a telemetry event (async wrapper)."""
    send_telemetry_event(event, component, outcome, **fields)
