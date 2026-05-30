"""Gateway management and relay operations."""

import base64
import hashlib
import json
import os
import pathlib
import secrets
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import webbrowser
from datetime import datetime, timedelta, timezone

from . import crypto


# Gateway state files
def gateway_state_path():
    return local_state_dir() / "gateway.json"


def gateway_schedules_state_path():
    return local_state_dir() / "gateway-schedules.json"


def gateway_relay_secret_path():
    return local_state_dir() / "gateway-relay.secret"


def local_state_dir():
    """Get the local state directory."""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~\\AppData\\Local")
        return pathlib.Path(base) / "napseer"
    return pathlib.Path.home() / ".napseer"


def read_gateway_relay_passphrase(create=False):
    """Read or create gateway relay passphrase."""
    value = os.environ.get("NAPSEER_GATEWAY_PASSPHRASE")
    if value:
        return value
    
    path = gateway_relay_secret_path()
    if path.exists():
        return path.read_text().strip()
    
    if create:
        passphrase = secrets.token_urlsafe(32)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(passphrase)
        path.chmod(0o600)
        return passphrase
    
    return None


def read_project_vault_passphrase(create=False):
    """Read or create project vault passphrase."""
    value = os.environ.get("NAPSEER_VAULT_PASSPHRASE")
    if value:
        return value
    
    path = local_state_dir() / "vault-passphrase.secret"
    if path.exists():
        return path.read_text().strip()
    
    if create:
        passphrase = secrets.token_urlsafe(32)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(passphrase)
        path.chmod(0o600)
        return passphrase
    
    return None


def vault_exists():
    """Check if vault file exists."""
    return (local_state_dir() / "vault.json").exists()


# Gateway status and management
def gateway_status():
    """Get gateway status."""
    state_path = gateway_state_path()
    if not state_path.exists():
        return {"running": False, "configured": False, "message": "gateway not configured"}
    
    try:
        state = json.loads(state_path.read_text())
        running = False
        pid = state.get("pid")
        if pid:
            try:
                os.kill(pid, 0)
                running = True
            except OSError:
                running = False
        
        return {
            "running": running,
            "configured": True,
            "pid": pid,
            "port": state.get("port"),
            "relay_lanes": state.get("relay_lanes", ["terminal", "chat"]),
        }
    except (json.JSONDecodeError, IOError):
        return {"running": False, "configured": False, "message": "invalid gateway state"}


def gateway_lock():
    """Acquire gateway lock file."""
    lock_path = local_state_dir() / "gateway.lock"
    try:
        lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(lock_fd)
        return True
    except FileExistsError:
        return False


def gateway_unlock():
    """Release gateway lock file."""
    lock_path = local_state_dir() / "gateway.lock"
    try:
        os.remove(lock_path)
    except FileExistsError:
        pass


def clear_gateway_runtime_caches():
    """Clear gateway runtime caches."""
    pass  # Placeholder for cache clearing


def touch_gateway():
    """Update gateway state timestamp."""
    state_path = gateway_state_path()
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text())
            state["last_seen"] = datetime.now(timezone.utc).isoformat()
            state_path.write_text(json.dumps(state))
        except (json.JSONDecodeError, IOError):
            pass


def gateway_is_unlocked():
    """Check if gateway is unlocked (has required secrets)."""
    return vault_exists() and read_gateway_relay_passphrase() is not None


def gateway_locked_reason():
    """Get reason why gateway is locked."""
    if not vault_exists():
        return "vault_not_initialized"
    if not read_gateway_relay_passphrase():
        return "relay_passphrase_not_configured"
    return None


# Relay operations
def derive_gateway_relay_secret(passphrase):
    """Derive relay secret from passphrase."""
    return hashlib.sha256(passphrase.encode("utf-8")).digest()


def write_gateway_relay_secret(passphrase):
    """Write gateway relay secret."""
    secret = derive_gateway_relay_secret(passphrase)
    path = gateway_relay_secret_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(base64.b64encode(secret).decode())
    path.chmod(0o600)


def read_gateway_relay_secret():
    """Read gateway relay secret."""
    path = gateway_relay_secret_path()
    if not path.exists():
        return None
    try:
        return base64.b64decode(path.read_text())
    except Exception:
        return None


def gateway_relay_secret_fingerprint(secret):
    """Get fingerprint of relay secret."""
    if not secret:
        return None
    return hashlib.sha256(secret).hexdigest()[:16]


def read_gateway_relay_state():
    """Read gateway relay state."""
    state_path = local_state_dir() / "gateway-relay.json"
    if not state_path.exists():
        return {}
    try:
        return json.loads(state_path.read_text())
    except (json.JSONDecodeError, IOError):
        return {}


def write_gateway_relay_state(**updates):
    """Update gateway relay state."""
    state_path = local_state_dir() / "gateway-relay.json"
    state = read_gateway_relay_state()
    state.update(updates)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state))


# Terminal management
def clean_terminal_name(value, fallback="terminal"):
    """Clean terminal name for safe use."""
    if not value:
        return fallback
    cleaned = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(value))
    return cleaned[:64] or fallback


def clean_tmux_name(value, fallback="napseer"):
    """Clean tmux session name."""
    if not value:
        return fallback
    cleaned = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(value))
    return cleaned[:200] or fallback


def current_tmux_target():
    """Get current tmux target."""
    return os.environ.get("TMUX_TARGET", os.environ.get("TMUX_PANE", None))


# Gateway periodic logging
def gateway_log(event, level="info", **fields):
    """Log gateway event."""
    timestamp = datetime.now(timezone.utc).isoformat()
    log_entry = {
        "timestamp": timestamp,
        "level": level,
        "event": event,
        **fields,
    }
    # Write to gateway log file
    log_path = local_state_dir() / "gateway.log"
    try:
        with open(log_path, "a") as f:
            f.write(json.dumps(log_entry) + "\n")
    except IOError:
        pass
    return log_entry


def gateway_periodic_log(key, interval_seconds, event, level="info", **fields):
    """Log gateway event with periodic throttle."""
    throttle_path = local_state_dir() / f"gateway-log-throttle-{key}"
    now = time.time()
    
    try:
        last_log = float(throttle_path.read_text().strip())
        if now - last_log < interval_seconds:
            return None
    except (IOError, ValueError):
        pass
    
    try:
        throttle_path.write_text(str(now))
    except IOError:
        pass
    
    return gateway_log(event, level, **fields)
