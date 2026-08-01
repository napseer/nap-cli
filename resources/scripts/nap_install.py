#!/usr/bin/env python3
"""Install the Napseer local command.

Usage:
  python3 nap_install.py
  nap init
  nap status
  nap doctor
  nap auth login
  nap project create
  nap project attach
  nap mcp status
  nap gateway start
  nap gateway stop
  nap update
  nap version
"""

import ast
import contextlib
import json
import hashlib
import os
import pathlib
import select
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


BASE_URL = os.environ.get("NAPSEER_BASE_URL", "https://api.napseer.com").rstrip("/")
INSTALL_DIR = pathlib.Path(os.environ.get("NAPSEER_HOME", pathlib.Path.home() / ".local" / "share" / "napseer"))
BIN_DIR = pathlib.Path(os.environ.get("NAPSEER_BIN_DIR", pathlib.Path.home() / ".local" / "bin"))
SCRIPT_NAMES = (
    "napseer_mcp_server.py",
    "napseer_mcp_supervisor.py",
    "napseer_spake2.py",
    "terminal_init.py",
    "terminal_protocol.py",
    "terminal_pty_manager.py",
    "nap_install.py",
)
HTTP_TIMEOUT_SECONDS = int(os.environ.get("NAPSEER_HTTP_TIMEOUT_SECONDS", "30"))
LOCAL_HOST = "127.0.0.1"
DEFAULT_GATEWAY_PORT = os.environ.get("NAPSEER_GATEWAY_PORT")
RUNTIME_SCRIPT_NAMES = (
    "napseer_spake2.py",
    "terminal_init.py",
    "terminal_protocol.py",
    "terminal_pty_manager.py",
    "napseer_mcp_server.py",
    "napseer_mcp_supervisor.py",
)
MAX_SERVICE_LOG_BYTES = int(os.environ.get("NAPSEER_MAX_SERVICE_LOG_BYTES", str(5 * 1024 * 1024)))
CLI_RELEASE_VERSION = "0.2.1"
CLI_DISTRIBUTION_CONTRACT_VERSION = "2026-08-01"
CLI_MINIMUM_CONTRACT_VERSION = "2026-05-19"
CLI_BUNDLE_SCHEMA_VERSION = "napseer.cli.bundle.v1"
INSTALL_PATHS = {
    "nap_install.py": "nap_install.py",
    "napseer_mcp_server.py": "napseer_mcp_server.py",
    "napseer_mcp_supervisor.py": "napseer_mcp_supervisor.py",
    "napseer_spake2.py": "napseer_spake2.py",
    "terminal_init.py": "terminal/__init__.py",
    "terminal_protocol.py": "terminal/protocol.py",
    "terminal_pty_manager.py": "terminal/pty_manager.py",
}
CLI_GENERATED_SOURCE_REPO = os.environ.get("NAPSEER_CLI_SOURCE_REPO", "https://github.com/napseer/nap-cli")
CLI_GENERATED_SOURCE_REVISION = os.environ.get("NAPSEER_CLI_SOURCE_REVISION", "unresolved")
CLI_GENERATED_SOURCE_REVISION_STATUS = os.environ.get("NAPSEER_CLI_SOURCE_REVISION_STATUS", "unresolved")
HELP_TOKENS = {"help", "-h", "--help"}
CANONICAL_COMMANDS = (
    ("init", "Initialize Napseer in the current folder."),
    ("status", "Show concise account, project, and runtime state."),
    ("doctor", "Diagnose local setup without changing it."),
    ("auth", "Authenticate an account or repair credentials."),
    ("project", "Create, attach, claim, or inspect a project."),
    ("mcp", "Install, update, inspect, or serve the local MCP runtime."),
    ("gateway", "Set up and operate the local gateway."),
    ("update", "Update the installed CLI and runtime bundle."),
    ("version", "Show version, source revision, and contract identity."),
    ("help", "Show this help or help for one command."),
)
COMMAND_METADATA = {
    "init": {"mutates": True, "visible": True},
    "status": {"mutates": False, "visible": True},
    "doctor": {"mutates": False, "visible": True},
    "auth": {"mutates": True, "visible": True},
    "project": {"mutates": True, "visible": True},
    "mcp": {"mutates": True, "visible": True},
    "gateway": {"mutates": True, "visible": True},
    "update": {"mutates": True, "visible": True},
    "version": {"mutates": False, "visible": True},
    "help": {"mutates": False, "visible": True},
}


def chmod_best_effort(path, mode):
    try:
        path.chmod(mode)
    except OSError:
        pass


def normalize_proxy_url(value):
    if not value:
        return None
    proxy_url = str(value).strip()
    if not proxy_url:
        return None
    if "://" not in proxy_url:
        proxy_url = f"http://{proxy_url}"
    return urllib.parse.urlparse(proxy_url)


def api_proxy_url(parsed):
    host = parsed.hostname or ""
    if host and urllib.request.proxy_bypass(host):
        return None
    explicit = (
        os.environ.get("NAPSEER_API_PROXY_URL")
        or os.environ.get("NAPSEER_HTTP_PROXY_URL")
        or os.environ.get("NAPSEER_PROXY_URL")
        or os.environ.get("NAPSEER_RELAY_PROXY_URL")
        or os.environ.get("NAPSEER_GATEWAY_PROXY_URL")
    )
    if explicit:
        return explicit
    proxies = urllib.request.getproxies_environment()
    return proxies.get(parsed.scheme) or proxies.get("all")


def api_urlopen(request, timeout):
    parsed = urllib.parse.urlparse(request.full_url)
    proxy = normalize_proxy_url(api_proxy_url(parsed))
    if not proxy:
        return urllib.request.urlopen(request, timeout=timeout)
    proxy_url = urllib.parse.urlunparse(proxy)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({parsed.scheme: proxy_url}))
    return opener.open(request, timeout=timeout)


def read_url_bytes(url, user_agent, label):
    request = urllib.request.Request(url, headers={"User-Agent": user_agent}, method="GET")
    try:
        with api_urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            return response.read()
    except (TimeoutError, socket.timeout) as exc:
        raise RuntimeError(f"{label} timed out after {HTTP_TIMEOUT_SECONDS}s") from exc
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{label} failed: HTTP {exc.code}: {body}") from exc


def fetch_json(path, label):
    try:
        body = read_url_bytes(
            f"{BASE_URL}{path}",
            "nap-install-python/0.1",
            f"GET {path}",
        )
        payload = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{label} response was not valid JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"{label} response must be a JSON object")
    return payload


def fetch_script(name):
    payload = fetch_json(f"/v1/scripts/{name}", name)
    content = payload.get("content")
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError(f"{name} response did not include script content")
    expected_sha256 = payload.get("sha256")
    if isinstance(expected_sha256, str) and expected_sha256.strip():
        actual_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if actual_sha256 != expected_sha256.lower():
            raise RuntimeError(
                f"{name} backend script sha256 mismatch: expected {expected_sha256.lower()}, got {actual_sha256}"
            )
    return payload, content


def write_file(path, content, executable=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    chmod_best_effort(path, 0o755 if executable else 0o644)


def path_warning():
    path_entries = os.environ.get("PATH", "").split(os.pathsep)
    if str(BIN_DIR) not in path_entries:
        return f"Add {BIN_DIR} to PATH to run nap from any directory."
    return None


def source_revision_status_for(value):
    if not isinstance(value, str):
        return "unresolved"
    revision = value.strip()
    if not revision or revision in ("unresolved", "unknown", "null"):
        return "unresolved"
    return "resolved"


def bundle_manifest():
    return fetch_json("/v1/scripts", "CLI bundle manifest")


def contract_version_tuple(value):
    parts = str(value or "").split("-")
    if len(parts) != 3 or any(not part.isdigit() for part in parts):
        return None
    year, month, day = (int(part) for part in parts)
    if year < 2000 or not 1 <= month <= 12 or not 1 <= day <= 31:
        return None
    return year, month, day


def release_version_tuple(value):
    parts = str(value or "").split(".")
    if len(parts) != 3 or any(not part.isdigit() for part in parts):
        return None
    return tuple(int(part) for part in parts)


def validated_manifest_items(manifest):
    if manifest.get("schema_version") != CLI_BUNDLE_SCHEMA_VERSION:
        raise RuntimeError(
            f"unsupported CLI bundle schema: {manifest.get('schema_version')!r}"
        )
    contract = manifest.get("contract")
    if not isinstance(contract, dict):
        raise RuntimeError("CLI bundle manifest is missing contract metadata")
    current_contract = contract_version_tuple(contract.get("current"))
    minimum_contract = contract_version_tuple(contract.get("minimum_supported"))
    installer_contract = contract_version_tuple(CLI_DISTRIBUTION_CONTRACT_VERSION)
    if current_contract is None or minimum_contract is None or current_contract < minimum_contract:
        raise RuntimeError("CLI bundle contract metadata is invalid")
    if installer_contract is None or installer_contract < minimum_contract:
        raise RuntimeError("CLI bundle requires a newer bootstrap installer")
    if release_version_tuple(manifest.get("release_version")) is None:
        raise RuntimeError("CLI bundle release version is invalid")
    source = manifest.get("source")
    if (
        not isinstance(source, dict)
        or source_revision_status_for(source.get("revision")) != "resolved"
    ):
        raise RuntimeError("CLI bundle source revision must be resolved")
    items = manifest.get("items")
    if not isinstance(items, list):
        raise RuntimeError("CLI bundle manifest items must be an array")
    by_name = {}
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            raise RuntimeError("CLI bundle manifest contains an invalid item")
        name = item["name"]
        if name in by_name:
            raise RuntimeError(f"CLI bundle manifest contains duplicate asset {name}")
        by_name[name] = item
    missing = [name for name in SCRIPT_NAMES if name not in by_name]
    if missing:
        raise RuntimeError(f"CLI bundle manifest is missing assets: {', '.join(missing)}")
    for name in SCRIPT_NAMES:
        item = by_name[name]
        expected_path = INSTALL_PATHS[name]
        path = pathlib.PurePosixPath(str(item.get("install_path") or ""))
        if str(path) != expected_path or path.is_absolute() or ".." in path.parts:
            raise RuntimeError(f"CLI bundle asset {name} has unsafe install_path")
        if item.get("mode") != "0755":
            raise RuntimeError(f"CLI bundle asset {name} has unsupported mode")
        if item.get("source_repo") != source.get("repo"):
            raise RuntimeError(f"CLI bundle asset {name} has inconsistent source repo")
        if item.get("source_revision") != source.get("revision"):
            raise RuntimeError(f"CLI bundle asset {name} has inconsistent source revision")
        if item.get("contract_version") != contract.get("current"):
            raise RuntimeError(f"CLI bundle asset {name} has inconsistent contract version")
        if item.get("minimum_contract_version") != contract.get("minimum_supported"):
            raise RuntimeError(f"CLI bundle asset {name} has inconsistent minimum contract")
        if not isinstance(item.get("bytes"), int) or item["bytes"] <= 0:
            raise RuntimeError(f"CLI bundle asset {name} has invalid byte length")
        digest = str(item.get("sha256") or "")
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise RuntimeError(f"CLI bundle asset {name} has invalid sha256")
    return {name: by_name[name] for name in SCRIPT_NAMES}


def verify_asset_payload(name, manifest_item, payload, content):
    actual = content.encode("utf-8")
    if len(actual) != manifest_item["bytes"]:
        raise RuntimeError(f"{name} byte length does not match bundle manifest")
    if hashlib.sha256(actual).hexdigest() != manifest_item["sha256"]:
        raise RuntimeError(f"{name} sha256 does not match bundle manifest")
    for field in (
        "version",
        "source_repo",
        "source_revision",
        "contract_version",
        "minimum_contract_version",
        "install_path",
        "mode",
    ):
        if payload.get(field) != manifest_item.get(field):
            raise RuntimeError(f"{name} {field} does not match bundle manifest")


def release_name(manifest):
    revision = manifest["source"]["revision"]
    identity = str(manifest.get("bundle_id") or "")
    suffix = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:8]
    return f"{manifest['release_version']}-{revision[:12]}-{suffix}"


def validate_staged_python(release_dir):
    for install_path in INSTALL_PATHS.values():
        path = release_dir / install_path
        if path.suffix == ".py":
            try:
                ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except (OSError, SyntaxError) as exc:
                raise RuntimeError(
                    f"staged Python asset failed validation: {install_path}"
                ) from exc


def validate_release_hashes(release_dir, items):
    for name, item in items.items():
        path = release_dir / INSTALL_PATHS[name]
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise RuntimeError(f"release asset is missing: {name}") from exc
        if len(content) != item["bytes"]:
            raise RuntimeError(f"release asset byte length is invalid: {name}")
        if hashlib.sha256(content).hexdigest() != item["sha256"]:
            raise RuntimeError(f"release asset sha256 is invalid: {name}")


def pid_exists(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


@contextlib.contextmanager
def installation_lock():
    INSTALL_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = INSTALL_DIR / ".install.lock"
    for attempt in range(2):
        try:
            descriptor = os.open(
                str(lock_path),
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(str(os.getpid()))
            break
        except FileExistsError:
            try:
                owner = int(lock_path.read_text(encoding="utf-8").strip())
            except (OSError, ValueError):
                owner = None
            if attempt == 0 and not pid_exists(owner):
                lock_path.unlink(missing_ok=True)
                continue
            raise RuntimeError("another Napseer install or update is already running")
    try:
        yield
    finally:
        lock_path.unlink(missing_ok=True)


def atomic_symlink(path, target):
    temporary = path.with_name(f".{path.name}.next-{os.getpid()}")
    temporary.unlink(missing_ok=True)
    os.symlink(target, temporary)
    os.replace(temporary, path)


def preserve_legacy_path(path):
    if path.is_symlink() or not path.exists():
        return
    backup = path.with_name(f"{path.name}.pre-bundle")
    if backup.exists() or backup.is_symlink():
        raise RuntimeError(
            f"cannot preserve legacy install path because backup exists: {backup}"
        )
    os.replace(path, backup)


def activate_release(release_dir):
    current = INSTALL_DIR / "current"
    if os.name == "nt":
        for install_path in INSTALL_PATHS.values():
            target = INSTALL_DIR / install_path
            content = (release_dir / install_path).read_text(encoding="utf-8")
            write_file(target, content, executable=True)
        nap_path = BIN_DIR / "nap"
        write_file(
            nap_path,
            (release_dir / "nap_install.py").read_text(encoding="utf-8"),
            executable=True,
        )
        write_file(
            BIN_DIR / "nap.cmd",
            f'@echo off\r\n"{sys.executable}" "{nap_path}" %*\r\n',
            executable=False,
        )
        return nap_path

    for filename in (
        "nap_install.py",
        "napseer_mcp_server.py",
        "napseer_mcp_supervisor.py",
        "napseer_spake2.py",
    ):
        path = INSTALL_DIR / filename
        preserve_legacy_path(path)
        atomic_symlink(path, f"current/{filename}")
    terminal_path = INSTALL_DIR / "terminal"
    preserve_legacy_path(terminal_path)
    atomic_symlink(terminal_path, "current/terminal")

    BIN_DIR.mkdir(parents=True, exist_ok=True)
    nap_path = BIN_DIR / "nap"
    preserve_legacy_path(nap_path)
    atomic_symlink(
        nap_path,
        os.path.relpath(INSTALL_DIR / "nap_install.py", BIN_DIR),
    )
    atomic_symlink(current, os.path.relpath(release_dir, INSTALL_DIR))
    return nap_path


def install_assets():
    with installation_lock():
        manifest = bundle_manifest()
        items = validated_manifest_items(manifest)
        releases_dir = INSTALL_DIR / "releases"
        releases_dir.mkdir(parents=True, exist_ok=True)
        final_release = releases_dir / release_name(manifest)
        staging = releases_dir / f".staging-{os.getpid()}-{time.time_ns()}"
        staging.mkdir(mode=0o700)
        scripts = {}
        try:
            for name in SCRIPT_NAMES:
                manifest_item = items[name]
                payload, content = fetch_script(name)
                verify_asset_payload(name, manifest_item, payload, content)
                target = staging / INSTALL_PATHS[name]
                write_file(target, content, executable=True)
                scripts[name] = {
                    "path": str(INSTALL_DIR / "current" / INSTALL_PATHS[name]),
                    "version": payload.get("version"),
                    "sha256": payload.get("sha256"),
                    "source_repo": payload.get("source_repo"),
                    "source_revision": payload.get("source_revision"),
                    "contract_version": payload.get("contract_version"),
                }
            validate_staged_python(staging)
            write_file(
                staging / "bundle-manifest.json",
                json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                executable=False,
            )
            if final_release.exists():
                shutil.rmtree(staging)
            else:
                os.replace(staging, final_release)
            validate_release_hashes(final_release, items)
            nap_path = activate_release(final_release)
        except Exception:
            if staging.exists():
                shutil.rmtree(staging)
            raise

    return {
        "status": "installed",
        "message": "Napseer CLI and runtime bundle installed atomically.",
        "bin": str(nap_path),
        "install_dir": str(INSTALL_DIR),
        "release_dir": str(final_release),
        "bundle_id": manifest.get("bundle_id"),
        "scripts": scripts,
        "cli_distribution": {
            "release_version": manifest.get("release_version"),
            "contract_version": manifest["contract"]["current"],
            "minimum_contract_version": manifest["contract"]["minimum_supported"],
            "source_repo": manifest["source"]["repo"],
            "source_revision": manifest["source"]["revision"],
            "source_revision_status": manifest["source"]["revision_status"],
        },
        "updated_project_paths": [],
        "path_warning": path_warning(),
        "next": ["nap init", "nap mcp status", "nap gateway setup"],
    }


def runtime_assets_missing():
    missing = []
    for name in RUNTIME_SCRIPT_NAMES:
        path = runtime_asset_path(name)
        if not path.exists() or path.stat().st_size == 0:
            missing.append(name)
    return missing


def active_install_root():
    current = INSTALL_DIR / "current"
    return current if current.exists() else INSTALL_DIR


def runtime_asset_path(name):
    return active_install_root() / INSTALL_PATHS.get(name, name)


def require_runtime_assets():
    missing = runtime_assets_missing()
    if missing:
        raise RuntimeError(
            "missing installed Napseer runtime scripts: "
            f"{', '.join(missing)}. Run `nap mcp install` to repair them."
        )


def run_wrapper(command_name, args):
    wrapper = runtime_asset_path("napseer_mcp_server.py")
    require_runtime_assets()
    return subprocess.call([sys.executable, str(wrapper), command_name, *args])


def preferred_state_dir():
    return pathlib.Path.cwd() / ".napseer"


def cwd_state_dir():
    preferred = preferred_state_dir()
    preferred.mkdir(exist_ok=True)
    chmod_best_effort(preferred, 0o700)
    return preferred


def state_dir_status():
    preferred = preferred_state_dir()
    return {
        "active_state_dir": str(preferred),
        "active_auth_path": str(preferred / "auth.json"),
        "state_dir": str(preferred),
        "auth_path": str(preferred / "auth.json"),
        "state_dir_exists": preferred.exists(),
        "state_dir_message": (
            ".napseer directory is active."
            if preferred.exists()
            else ".napseer directory is not initialized."
        ),
    }


def service_state_path(kind):
    return cwd_state_dir() / f"{kind}.json"


def service_log_path(kind):
    return cwd_state_dir() / f"{kind}.log"


def read_service_state(kind):
    path = service_state_path(kind)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def write_service_state(kind, state):
    path = service_state_path(kind)
    write_file(path, json.dumps(state, indent=2), executable=False)
    chmod_best_effort(path, 0o600)


def pid_running(pid):
    if not pid:
        return False
    proc_stat = pathlib.Path("/proc") / str(pid) / "stat"
    try:
        fields = proc_stat.read_text(encoding="utf-8").split()
        if len(fields) > 2 and fields[2] == "Z":
            return False
    except Exception:
        pass
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def find_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((LOCAL_HOST, 0))
        return int(sock.getsockname()[1])


def port_accepts_connections(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex((LOCAL_HOST, int(port))) == 0


def port_can_bind(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((LOCAL_HOST, int(port)))
            return True
        except OSError:
            return False


def local_gateway_status(port):
    request = urllib.request.Request(
        f"http://{LOCAL_HOST}:{int(port)}/gateway/status",
        headers={"User-Agent": "nap-cli-python/0.1"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=1) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception:
        return None


def log_tail(path, max_chars=2000):
    try:
        text = pathlib.Path(path).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    return text[-max_chars:]


def log_tail_lines(path, max_lines=120):
    try:
        lines = pathlib.Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return []
    except Exception as exc:
        return [f"failed to read log: {exc}"]
    return lines[-max(1, max_lines):]


def compact_log_file(path, max_bytes=MAX_SERVICE_LOG_BYTES):
    if max_bytes <= 0:
        return
    file_path = pathlib.Path(path)
    try:
        size = file_path.stat().st_size
    except FileNotFoundError:
        return
    except OSError:
        return
    if size <= max_bytes:
        return
    try:
        with file_path.open("rb") as handle:
            handle.seek(-max_bytes, os.SEEK_END)
            chunk = handle.read(max_bytes)
    except OSError:
        return
    # Try to start on a newline boundary so the log stays line-oriented.
    newline_index = chunk.find(b"\n")
    if newline_index >= 0 and newline_index + 1 < len(chunk):
        chunk = chunk[newline_index + 1 :]
    try:
        # Preserve the inode used by a running gateway process. Replacing the
        # path here would leave its inherited stdout/stderr descriptors
        # writing to a deleted file.
        with file_path.open("r+b") as handle:
            handle.seek(0)
            handle.write(chunk)
            handle.truncate()
            handle.flush()
    except OSError:
        return


def ensure_port_available(kind, port):
    if not port_accepts_connections(port) and port_can_bind(port):
        return
    status = local_gateway_status(port)
    if status:
        cwd = status.get("cwd") or "unknown cwd"
        project_id = status.get("project_id") or "no project"
        raise RuntimeError(
            f"{LOCAL_HOST}:{port} is already serving a Napseer gateway for {cwd} "
            f"(project {project_id}). Stop that gateway before starting this one."
        )
    raise RuntimeError(f"{LOCAL_HOST}:{port} is already in use or still being released")


def choose_service_port(kind, args, existing=None):
    requested = cli_option(args, "--port", default=None)
    if requested is not None:
        return int(requested)
    if kind == "gateway":
        if DEFAULT_GATEWAY_PORT:
            return int(DEFAULT_GATEWAY_PORT)
        existing_port = (existing or {}).get("port")
        if existing_port and port_can_bind(existing_port):
            return int(existing_port)
        return find_free_port()
    return find_free_port()


def wait_for_service(kind, process, port, log_path):
    deadline = time.time() + 3
    while time.time() < deadline:
        if process.poll() is not None:
            tail = log_tail(log_path)
            detail = f": {tail.strip()}" if tail.strip() else ""
            raise RuntimeError(f"{kind} service exited during startup{detail}")
        if kind == "gateway" and local_gateway_status(port):
            return
        if kind != "gateway" and port_accepts_connections(port):
            return
        time.sleep(0.1)
    raise RuntimeError(f"{kind} service did not start listening on {LOCAL_HOST}:{port}")


def cli_option(args, *names, default=None):
    for name in names:
        if name in args:
            index = args.index(name)
            if index + 1 >= len(args):
                raise RuntimeError(f"{name} requires a value")
            return args[index + 1]
    return default


def start_service(kind, args):
    wrapper = runtime_asset_path("napseer_mcp_server.py")
    require_runtime_assets()
    existing = read_service_state(kind)
    if existing and pid_running(existing.get("pid")):
        return {
            **existing,
            "status": "running",
            "message": f"{kind} service is already running.",
        }
    port = choose_service_port(kind, args, existing)
    ensure_port_available(kind, port)
    log_path = service_log_path(kind)
    compact_log_file(log_path)
    command = [sys.executable, str(wrapper), "open-ui", "--port", str(port), "--no-browser"]
    if kind == "ui" and "--no-browser" not in args:
        command.remove("--no-browser")
    with log_path.open("ab") as log:
        environment = os.environ.copy()
        gateway_auth_path = preferred_state_dir() / "gateway-auth.json"
        if kind == "gateway" and gateway_auth_path.is_file():
            environment["NAPSEER_AUTH_FILE"] = str(gateway_auth_path)
            environment["NAPSEER_PROJECT_ROOT"] = str(pathlib.Path.cwd())
        kwargs = {
            "stdout": log,
            "stderr": subprocess.STDOUT,
            "cwd": str(pathlib.Path.cwd()),
            "env": environment,
        }
        if os.name != "nt":
            kwargs["start_new_session"] = True
        else:
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        process = subprocess.Popen(command, **kwargs)
    wait_for_service(kind, process, port, log_path)
    state = {
        "status": "running",
        "kind": kind,
        "pid": process.pid,
        "host": LOCAL_HOST,
        "port": port,
        "url": f"http://{LOCAL_HOST}:{port}/",
        "cwd": str(pathlib.Path.cwd()),
        "log_path": str(log_path),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    write_service_state(kind, state)
    return {**state, "message": f"{kind} service started."}


def stop_service(kind):
    state = read_service_state(kind)
    if not state:
        return {"status": "stopped", "kind": kind}
    pid = state.get("pid")
    if pid_running(pid):
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            try:
                os.killpg(int(pid), signal.SIGTERM)
            except OSError:
                try:
                    os.kill(int(pid), signal.SIGTERM)
                except OSError:
                    pass
        deadline = time.time() + 5
        while time.time() < deadline and pid_running(pid):
            time.sleep(0.1)
        if pid_running(pid):
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                try:
                    os.killpg(int(pid), signal.SIGKILL)
                except OSError:
                    try:
                        os.kill(int(pid), signal.SIGKILL)
                    except OSError:
                        pass
            deadline = time.time() + 2
            while time.time() < deadline and pid_running(pid):
                time.sleep(0.1)
    port = state.get("port")
    if port:
        deadline = time.time() + 2
        while time.time() < deadline and not port_can_bind(port):
            time.sleep(0.1)
    stopped = not pid_running(pid) and (not port or port_can_bind(port))
    state = {**state, "status": "stopped" if stopped else "stopping"}
    write_service_state(kind, state)
    if not stopped:
        state["warning"] = f"{kind} process or port {port} is still being released"
        state["message"] = state["warning"]
    else:
        state["message"] = f"{kind} service stopped."
    return state


def list_service(kind):
    state = read_service_state(kind) or {
        "kind": kind,
        "host": LOCAL_HOST,
        "port": None,
        "url": None,
        "cwd": str(pathlib.Path.cwd()),
    }
    state["status"] = "running" if pid_running(state.get("pid")) else "stopped"
    state["message"] = f"{kind} service is {state['status']}."
    return state


def service_logs(kind, args):
    max_lines = int(cli_option(args, "--lines", "--tail", default="120"))
    path = service_log_path(kind)
    lines = log_tail_lines(path, max_lines=max_lines)
    status = "ok" if path.exists() else "missing"
    return {
        "status": status,
        "message": f"{kind} log {'read' if path.exists() else 'does not exist yet'}.",
        "kind": kind,
        "log_path": str(path),
        "line_count": len(lines),
        "lines": lines,
        "text": "\n".join(lines),
    }


def handle_gateway(args):
    subcommand = args[0] if args else "status"
    rest = args[1:] if args else []
    if subcommand in {"help", "-h", "--help"}:
        return print_gateway_help()
    if subcommand in {"terminal", "schedule", "vault"} and rest and rest[0] in {"help", "-h", "--help"}:
        return print_gateway_help(subcommand)
    if subcommand == "vault" and rest and rest[0] in {"list", "ls"}:
        raise RuntimeError("unknown gateway vault command: list; use `nap gateway vault status`")
    if subcommand == "process":
        raise RuntimeError("unknown gateway command: process; use `nap gateway vault process`")
    if subcommand == "start":
        return print_json(start_service("gateway", ["--no-browser", *rest])) or 0
    if subcommand == "stop":
        return print_json(stop_service("gateway")) or 0
    if subcommand == "status":
        return print_json(list_service("gateway")) or 0
    if subcommand == "logs":
        return print_json(service_logs("gateway", rest)) or 0
    if subcommand == "configure":
        raise RuntimeError("unknown gateway command: configure; use `nap gateway setup --command CMD`")
    if subcommand in {"restart", "kill"}:
        raise RuntimeError(f"unknown gateway command: {subcommand}; use start, stop, or terminal operations")
    if subcommand == "service":
        raise RuntimeError("unknown gateway command: service; use `nap gateway repair` then `nap gateway start`")
    if subcommand in {"setup", "repair", "vault", "terminal", "schedule"}:
        return run_wrapper("gateway", [subcommand, *rest])
    raise RuntimeError("unknown gateway command; use nap gateway start|stop|status|logs|setup|repair|terminal|schedule|vault")


def print_gateway_help(topic=None):
    if topic == "terminal":
        print("""Usage:
  nap gateway terminal list
  nap gateway terminal create [--name NAME] [--command CMD]
  nap gateway terminal capture --terminal ID [--start OFFSET]
  nap gateway terminal input --terminal ID --text TEXT
  nap gateway terminal key --terminal ID (--key KEY | --text TEXT)
  nap gateway terminal close [ID | --terminal ID]""")
        return 0
    if topic == "schedule":
        print("""Usage:
  nap gateway schedule list
  nap gateway schedule create --name NAME --terminal ID --message TEXT --cron EXPR
  nap gateway schedule update [ID | --schedule ID] [--name NAME] [--message TEXT] [--cron EXPR] [--enabled | --disabled]
  nap gateway schedule run [ID | --schedule ID]
  nap gateway schedule delete [ID | --schedule ID]""")
        return 0
    if topic == "vault":
        print("Usage: nap gateway vault [status|process|rotate-secret] [--kind memory] [--all] [--project-id ID] [--vault-passphrase TEXT]")
        print("  status        Show local gateway state and pending setup requests.")
        print("  process       Upload opaque client-wrapped key bundle records for backend-owned HashiCorp storage.")
        print("  rotate-secret Rotate the memory project secret from the gateway worker path.")
        return 0
    print("Usage: nap gateway [start|stop|status|logs|setup|repair|terminal|schedule|vault]")
    print("  nap gateway start [--port PORT]       Start the local gateway service.")
    print("  nap gateway status                    Show the managed gateway process.")
    print("  nap gateway setup [--command CMD]     Create local storage and set the default command.")
    print("  nap gateway repair                    Create dedicated gateway worker credentials.")
    print("  nap gateway terminal help             Show terminal lifecycle commands.")
    print("  nap gateway schedule help             Show schedule lifecycle commands.")
    print("  nap gateway vault help                Show vault commands.")
    return 0


def print_json(value):
    print(json.dumps(value, indent=2))


def print_root_help():
    print("Usage: nap <command> [arguments]")
    print()
    print("Commands:")
    width = max(len(name) for name, _summary in CANONICAL_COMMANDS)
    for name, summary in CANONICAL_COMMANDS:
        print(f"  {name.ljust(width)}  {summary}")
    print()
    print("Run `nap help <command>` for command-specific help.")


def print_command_help(command):
    help_text = {
        "init": (
            "Usage: nap init [--slug SLUG] [--name NAME] [--description TEXT]\n"
            "Initialize the current folder and create its first Napseer project."
        ),
        "status": (
            "Usage: nap status\n"
            "Show the current folder's account, project, and runtime state."
        ),
        "doctor": (
            "Usage: nap doctor\n"
            "Run read-only checks and print exact repair commands."
        ),
        "auth": (
            "Usage: nap auth [status|login|repair]\n"
            "  login   Authenticate an account; does not select a project.\n"
            "  status  Show credential state without credential values.\n"
            "  repair  Recover failed automatic credential renewal."
        ),
        "project": (
            "Usage: nap project [init|create|attach|claim|status|encryption]\n"
            "  init    Initialize a fresh folder (same workflow as `nap init`).\n"
            "  create  Create and bind a new project.\n"
            "  attach  Select an existing project owned by the logged-in account.\n"
            "  claim   Move an anonymous project into an authenticated account.\n"
            "  status  Show the current folder binding."
        ),
        "mcp": (
            "Usage: nap mcp [status|install|update|serve]\n"
            "  status   Show supervisor and worker installation state.\n"
            "  install  Install or repair local MCP assets.\n"
            "  update   Update local MCP assets from verified published sources.\n"
            "  serve    Run the stable stdio supervisor."
        ),
        "gateway": (
            "Usage: nap gateway [start|stop|status|logs|setup|repair|terminal|schedule|vault]\n"
            "Use setup once, repair only when worker credentials are missing or revoked,\n"
            "then operate the daemon through terminal, schedule, and vault."
        ),
        "update": (
            "Usage: nap update\n"
            "Update the installed CLI and runtime bundle. Help never performs an update."
        ),
        "version": (
            "Usage: nap version\n"
            "Show distribution contract and canonical source revision."
        ),
        "install": "Compatibility command. Use `nap mcp install`.",
        "where": "Compatibility command. Use `nap doctor` or `nap mcp status`.",
        "chat": "Compatibility command. Chat-secret operations are an advanced runtime workflow.",
        "plan": "Compatibility command. Agent planning belongs to the authenticated MCP surface.",
        "lineage": "Compatibility command. Agent lineage checks belong to the authenticated MCP surface.",
        "agent": "Compatibility command. Agent workspace operations belong to the authenticated MCP surface.",
        "feedback": "Compatibility command. Feedback administration is not a primary operator workflow.",
    }
    if command in {"help", "-h", "--help", ""}:
        print_root_help()
        return 0
    text = help_text.get(command)
    if text is None:
        raise RuntimeError(f"unknown help topic: {command}")
    print(text)
    return 0


def mcp_status():
    worker = runtime_asset_path("napseer_mcp_server.py")
    supervisor = runtime_asset_path("napseer_mcp_supervisor.py")
    missing = runtime_assets_missing()
    probe = mcp_runtime_probe() if not missing else {"status": "not_run", "transport": False, "read": False}
    return {
        "status": "probe_ok" if not missing and probe.get("status") == "ok" else "repair_required",
        "supervisor_installed": supervisor.is_file(),
        "worker_installed": worker.is_file(),
        "runtime_assets_missing": missing,
        "fresh_process_probe": probe,
        "client_connection": "not_observable",
        "message": (
            "A fresh MCP supervisor transport and authenticated read succeeded. Existing client connections are separate; restart the client if it reports Transport closed."
            if probe.get("status") == "ok"
            else "The installed MCP runtime did not pass a fresh-process probe."
        ),
        "serve_command": f"{sys.executable} {supervisor}",
        "next": None if not missing else "nap mcp install",
    }


def mcp_runtime_probe(timeout_seconds=12):
    supervisor = runtime_asset_path("napseer_mcp_supervisor.py")
    worker = runtime_asset_path("napseer_mcp_server.py")
    if not supervisor.is_file() or not worker.is_file():
        return {"status": "not_installed", "transport": False, "read": False}
    environment = os.environ.copy()
    environment["NAPSEER_PROJECT_ROOT"] = str(pathlib.Path.cwd())
    process = subprocess.Popen(
        [sys.executable, str(supervisor)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        env=environment,
    )

    def request(request_id, method, params=None):
        message = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        process.stdin.write(json.dumps(message) + "\n")
        process.stdin.flush()
        ready, _, _ = select.select([process.stdout], [], [], timeout_seconds)
        if not ready:
            raise TimeoutError("MCP probe timed out")
        return json.loads(process.stdout.readline())

    try:
        initialized = request(1, "initialize", {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "nap-doctor", "version": CLI_RELEASE_VERSION},
        })
        listed = request(2, "tools/list")
        read = request(3, "tools/call", {"name": "nap_library_ls", "arguments": {}})
        tools = ((listed.get("result") or {}).get("tools") or [])
        read_result = read.get("result") or {}
        read_ok = not bool(read_result.get("isError"))
        initialized_ok = bool(initialized.get("result"))
        return {
            "status": "ok" if initialized_ok and tools and read_ok else "failed",
            "transport": bool(initialized_ok and tools),
            "read": read_ok,
            "tool_count": len(tools),
        }
    except Exception as exc:
        return {
            "status": "failed",
            "transport": False,
            "read": False,
            "error_class": exc.__class__.__name__,
        }
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)


def handle_mcp(args):
    subcommand = args[0] if args else "status"
    rest = args[1:]
    if subcommand == "status":
        print_json(mcp_status())
        return 0
    if subcommand == "install":
        print_json(install_assets())
        return 0
    if subcommand == "update":
        result = install_assets()
        print_json({**result, "status": "updated", "message": "Napseer MCP runtime is updated."})
        return 0
    if subcommand == "serve":
        if rest:
            raise RuntimeError("nap mcp serve does not accept arguments")
        require_runtime_assets()
        supervisor = runtime_asset_path("napseer_mcp_supervisor.py")
        os.execv(sys.executable, [sys.executable, str(supervisor)])
    raise RuntimeError("unknown MCP command; use nap mcp status|install|update|serve")


def doctor_status():
    state = state_dir_status()
    auth_path = pathlib.Path(state["active_auth_path"])
    auth = {}
    auth_error = None
    if auth_path.exists():
        try:
            auth = json.loads(auth_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            auth_error = "auth file is unreadable or invalid JSON"
    missing = runtime_assets_missing()
    issues = []
    if auth_error:
        issues.append({"code": "auth_file_invalid", "message": auth_error, "next": "nap auth login"})
    elif not auth_path.exists():
        issues.append({"code": "project_not_initialized", "message": "current folder is not initialized", "next": "nap init"})
    elif not auth.get("project_id"):
        issues.append({"code": "project_not_attached", "message": "no project is attached", "next": "nap project attach"})
    if missing:
        issues.append({"code": "runtime_assets_missing", "message": "local MCP runtime is incomplete", "next": "nap mcp install"})
    if not runtime_asset_path("napseer_mcp_supervisor.py").is_file():
        issues.append({"code": "supervisor_missing", "message": "stable MCP supervisor is not installed", "next": "nap mcp install"})
    probe = mcp_runtime_probe() if not missing else {"status": "not_run", "transport": False, "read": False}
    if not missing and probe.get("status") != "ok":
        issues.append({
            "code": "mcp_fresh_probe_failed",
            "message": "fresh MCP transport or authenticated read failed",
            "next": "nap mcp update; then restart the MCP client",
        })
    return {
        "status": "ok" if not issues else "repair_required",
        "checks": {
            "state_directory": state["state_dir_exists"],
            "auth_file": auth_path.exists() and auth_error is None,
            "account_mode": auth.get("account_mode"),
            "token_present": bool(auth.get("token")),
            "refresh_present": bool(auth.get("refresh_token")),
            "project_attached": bool(auth.get("project_id")),
            "mcp_supervisor": runtime_asset_path("napseer_mcp_supervisor.py").is_file(),
            "mcp_worker": runtime_asset_path("napseer_mcp_server.py").is_file(),
            "mcp_fresh_probe": probe,
            "client_connection": "not_observable",
        },
        "issues": issues,
    }


def version_status():
    manifest_path = active_install_root() / "bundle-manifest.json"
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            source = manifest.get("source") or {}
            contract = manifest.get("contract") or {}
            return {
                "release_version": manifest.get("release_version"),
                "contract_version": contract.get("current"),
                "minimum_contract_version": contract.get("minimum_supported"),
                "source_repo": source.get("repo"),
                "source_revision": source.get("revision"),
                "source_revision_status": source.get("revision_status"),
                "bundle_id": manifest.get("bundle_id"),
            }
        except (OSError, json.JSONDecodeError):
            pass
    return {
        "release_version": CLI_RELEASE_VERSION,
        "contract_version": CLI_DISTRIBUTION_CONTRACT_VERSION,
        "minimum_contract_version": CLI_MINIMUM_CONTRACT_VERSION,
        "source_repo": CLI_GENERATED_SOURCE_REPO,
        "source_revision": None
        if source_revision_status_for(CLI_GENERATED_SOURCE_REVISION) == "unresolved"
        else CLI_GENERATED_SOURCE_REVISION,
        "source_revision_status": source_revision_status_for(CLI_GENERATED_SOURCE_REVISION),
    }


def main(argv):
    invoked_as_nap = pathlib.Path(argv[0]).name in {"nap", "nap.cmd"}
    command = argv[1] if len(argv) > 1 else ("help" if invoked_as_nap else "install")
    args = argv[2:]
    if command in HELP_TOKENS:
        return print_command_help(args[0] if args else "")
    if any(item in HELP_TOKENS for item in args):
        if command == "gateway":
            topic = next((item for item in args if item not in HELP_TOKENS), None)
            return print_gateway_help(topic)
        return print_command_help(command)
    if command == "init":
        return run_wrapper("project", ["init", *args])
    if command == "doctor":
        print_json(doctor_status())
        return 0
    if command == "mcp":
        return handle_mcp(args)
    if command == "version":
        print_json(version_status())
        return 0
    if command == "update":
        result = install_assets()
        print_json({**result, "status": "updated", "message": "Napseer CLI and runtime scripts are updated."})
        return 0
    if command == "gateway":
        return handle_gateway(args)
    if command == "status":
        return run_wrapper("configure", args)
    if command == "project":
        if not args:
            return run_wrapper("project", ["status"])
        if args[0] == "bootstrap":
            raise RuntimeError("unknown project command: bootstrap")
        return run_wrapper("project", args)
    if command == "auth":
        if args and args[0] == "repair":
            return run_wrapper("auth", ["repair", *args[1:]])
        if args and args[0] == "refresh":
            raise RuntimeError("unknown auth command: refresh; use `nap auth repair`")
        return run_wrapper("auth", args)
    removed = {
        "install": "use `nap mcp install`",
        "where": "use `nap doctor` or `nap mcp status`",
        "chat": "use the authenticated MCP chat-secret tools",
        "plan": "use the authenticated MCP planning tools",
        "lineage": "use the authenticated MCP lineage tools",
        "agent": "use the authenticated MCP agent tools",
        "feedback": "use the authenticated MCP feedback tools",
    }
    if command in removed:
        raise RuntimeError(f"unknown command: {command}; {removed[command]}")
    raise RuntimeError(f"unknown command: {command}")


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv))
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, indent=2), file=sys.stderr)
        raise SystemExit(1)
