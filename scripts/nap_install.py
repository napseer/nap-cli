#!/usr/bin/env python3
"""Install the Napseer local command.

Usage:
  python3 nap_install.py
  nap project create
  nap project status
  nap gateway start
  nap gateway stop
  nap gateway configure
  nap gateway logs
  nap status
  nap agent list
  nap update
"""

import json
import hashlib
import os
import pathlib
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
)
MAX_SERVICE_LOG_BYTES = int(os.environ.get("NAPSEER_MAX_SERVICE_LOG_BYTES", str(5 * 1024 * 1024)))
CLI_DISTRIBUTION_CONTRACT_VERSION = "2026-05-03"
CLI_GENERATED_SOURCE_REPO = os.environ.get("NAPSEER_CLI_SOURCE_REPO", "https://github.com/napseer/nap-cli")
CLI_GENERATED_SOURCE_REVISION = os.environ.get("NAPSEER_CLI_SOURCE_REVISION", "unresolved")
CLI_GENERATED_SOURCE_REVISION_STATUS = os.environ.get("NAPSEER_CLI_SOURCE_REVISION_STATUS", "unresolved")


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


def fetch_script(name):
    try:
        body = read_url_bytes(
            f"{BASE_URL}/v1/scripts/{name}",
            "nap-install-python/0.1",
            f"GET /v1/scripts/{name}",
        )
        payload = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{name} response was not valid JSON") from exc
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


def install_assets(update_project=False):
    scripts = {}
    fetched = {}
    for name in SCRIPT_NAMES:
        payload, content = fetch_script(name)
        fetched[name] = (payload, content)

    install_order = []
    for name in (*RUNTIME_SCRIPT_NAMES, "nap_install.py", *SCRIPT_NAMES):
        if name not in install_order:
            install_order.append(name)
    for name in install_order:
        payload, content = fetched[name]
        target = INSTALL_DIR / str(payload.get("filename") or name)
        write_file(target, content, executable=name.endswith(".py"))
        scripts[name] = {
            "path": str(target),
            "version": payload.get("version"),
            "build_commit": payload.get("build_commit"),
            "sha256": payload.get("sha256"),
            "source_repo": payload.get("source_repo"),
            "source_revision": payload.get("source_revision"),
            "source_revision_status": payload.get("source_revision_status")
            or source_revision_status_for(payload.get("source_revision")),
            "generated_at": payload.get("generated_at"),
            "generator_version": payload.get("generator_version"),
            "contract_version": payload.get("contract_version"),
            "supported_backend_api_versions": payload.get("supported_backend_api_versions"),
            "supported_gateway_contract_versions": payload.get("supported_gateway_contract_versions"),
        }

    installer_content = (INSTALL_DIR / "nap_install.py").read_text(encoding="utf-8")
    nap_path = BIN_DIR / "nap"
    write_file(nap_path, installer_content, executable=True)
    if os.name == "nt":
        write_file(BIN_DIR / "nap.cmd", f'@echo off\r\n"{sys.executable}" "{nap_path}" %*\r\n', executable=False)

    updated_project_paths = []
    if update_project:
        for script_name in ("napseer_mcp_server.py", "napseer_spake2.py"):
            content = (INSTALL_DIR / script_name).read_text(encoding="utf-8")
            for candidate in (
                pathlib.Path.cwd() / script_name,
                pathlib.Path.cwd() / "resources" / "scripts" / script_name,
            ):
                if candidate.exists():
                    write_file(candidate, content, executable=script_name.endswith(".py"))
                    updated_project_paths.append(str(candidate))

    installer_payload = fetched["nap_install.py"][0]
    cli_source_revision = installer_payload.get("source_revision") or CLI_GENERATED_SOURCE_REVISION
    cli_source_revision_status = (
        installer_payload.get("source_revision_status")
        or CLI_GENERATED_SOURCE_REVISION_STATUS
        or source_revision_status_for(cli_source_revision)
    )
    result = {
        "status": "installed",
        "message": "Napseer CLI and runtime scripts are installed.",
        "bin": str(nap_path),
        "install_dir": str(INSTALL_DIR),
        "scripts": scripts,
        "cli_distribution": {
            "contract_version": CLI_DISTRIBUTION_CONTRACT_VERSION,
            "source_repo": installer_payload.get("source_repo") or CLI_GENERATED_SOURCE_REPO,
            "source_revision": None
            if source_revision_status_for(cli_source_revision) == "unresolved"
            else cli_source_revision,
            "source_revision_status": cli_source_revision_status,
        },
        "updated_project_paths": updated_project_paths,
        "path_warning": path_warning(),
        "next": ["nap project create", "nap gateway configure", "nap gateway start", "nap gateway logs", "nap update"],
    }
    return result


def runtime_assets_missing():
    missing = []
    for name in RUNTIME_SCRIPT_NAMES:
        path = INSTALL_DIR / {
            "terminal_init.py": "terminal/__init__.py",
            "terminal_protocol.py": "terminal/protocol.py",
            "terminal_pty_manager.py": "terminal/pty_manager.py",
        }.get(name, name)
        if not path.exists() or path.stat().st_size == 0:
            missing.append(name)
    return missing


def ensure_runtime_assets():
    missing = runtime_assets_missing()
    if missing:
        install_assets(update_project=False)
        missing = runtime_assets_missing()
    if missing:
        raise RuntimeError(f"missing installed Napseer runtime scripts after repair: {', '.join(missing)}")


def run_wrapper(command_name, args):
    wrapper = INSTALL_DIR / "napseer_mcp_server.py"
    ensure_runtime_assets()
    return subprocess.call([sys.executable, str(wrapper), command_name, *args])


def preferred_state_dir():
    return pathlib.Path.cwd() / ".napseer"


def legacy_state_dir():
    return pathlib.Path.cwd() / "napseer"


def cwd_state_dir():
    preferred = preferred_state_dir()
    legacy = legacy_state_dir()
    path = preferred if preferred.exists() or not legacy.exists() else legacy
    path.mkdir(exist_ok=True)
    chmod_best_effort(path, 0o700)
    return path


def state_dir_status():
    preferred = preferred_state_dir()
    legacy = legacy_state_dir()
    active = cwd_state_dir()
    if preferred.exists() and legacy.exists():
        message = "Both .napseer and legacy napseer directories exist; .napseer is active."
    elif active == legacy:
        message = "Legacy napseer directory is active because .napseer does not exist."
    else:
        message = ".napseer directory is active."
    return {
        "active_state_dir": str(active),
        "active_auth_path": str(active / "auth.json"),
        "preferred_state_dir": str(preferred),
        "preferred_auth_path": str(preferred / "auth.json"),
        "legacy_state_dir": str(legacy),
        "legacy_auth_path": str(legacy / "auth.json"),
        "preferred_state_dir_exists": preferred.exists(),
        "legacy_state_dir_exists": legacy.exists(),
        "legacy_state_dir_active": active == legacy,
        "state_dir_message": message,
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
    tmp_path = file_path.with_suffix(file_path.suffix + ".tmp")
    try:
        tmp_path.write_bytes(chunk)
        tmp_path.replace(file_path)
    except OSError:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass


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
    wrapper = INSTALL_DIR / "napseer_mcp_server.py"
    ensure_runtime_assets()
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
        kwargs = {"stdout": log, "stderr": subprocess.STDOUT, "cwd": str(pathlib.Path.cwd())}
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
    compact_log_file(path)
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
    subcommand = args[0] if args else "ls"
    rest = args[1:] if args else []
    if subcommand in {"help", "-h", "--help"}:
        print("Usage: nap gateway [start|stop|status|logs|configure|setup|unlock|lock|restart|kill|vault|terminal|schedule|service]")
        print("  nap gateway start [--port PORT]       Start the local gateway service.")
        print("  nap gateway status                    Show the managed gateway process.")
        print("  nap gateway configure [--command CMD] Configure the local gateway command.")
        print("  nap gateway setup                     Create or unlock the encrypted gateway vault.")
        print("  nap gateway vault                     Show gateway vault status and pending setup requests.")
        print("  nap gateway vault process             Complete pending setup requests.")
        print("  nap gateway vault rotate-secret --kind chat|tabs|gateway")
        return 0
    if subcommand in {"vault", "vault-setup", "vault_setup", "project-vault"} and rest and rest[0] in {"help", "-h", "--help"}:
        print("Usage: nap gateway vault [status|list|process|rotate-secret] [--kind chat|tabs|gateway] [--all] [--project-id ID]")
        print("  status        Show configured/locked state and pending setup requests.")
        print("  process       Generate local secret material and send encrypted envelopes only.")
        print("  rotate-secret Rotate one encrypted project secret.")
        print("  vault-setup   Deprecated alias for vault.")
        return 0
    if subcommand == "start":
        return print_json(start_service("gateway", ["--no-browser", *rest])) or 0
    if subcommand == "stop":
        return print_json(stop_service("gateway")) or 0
    if subcommand in {"ls", "list", "status"}:
        return print_json(list_service("gateway")) or 0
    if subcommand in {"logs", "log"}:
        return print_json(service_logs("gateway", rest)) or 0
    if subcommand in {"configure", "config"}:
        return run_wrapper("gateway", ["configure", *rest])
    if subcommand in {"setup", "unlock", "lock", "restart", "kill", "vault", "vault-setup", "vault_setup", "project-vault", "terminal", "schedule", "service"}:
        return run_wrapper("gateway", [subcommand, *rest])
    raise RuntimeError("unknown gateway command; use nap gateway start|stop|configure|setup|unlock|lock|restart|kill|logs|status|vault|terminal|schedule|service")


def print_json(value):
    print(json.dumps(value, indent=2))


def main(argv):
    invoked_as_nap = pathlib.Path(argv[0]).name in {"nap", "nap.cmd"}
    command = argv[1] if len(argv) > 1 else ("help" if invoked_as_nap else "install")
    args = argv[2:]
    if command == "install":
        print_json(install_assets(update_project=False))
        return 0
    if command == "update":
        result = install_assets(update_project=True)
        print_json({**result, "status": "updated", "message": "Napseer CLI and runtime scripts are updated."})
        return 0
    if command == "bootstrap":
        raise RuntimeError("nap bootstrap was removed; use nap project create")
    if command == "ui":
        raise RuntimeError("top-level nap ui was removed; use project and gateway commands instead")
    if command == "gateway":
        return handle_gateway(args)
    if command in {"configure", "config", "status"}:
        return run_wrapper("configure", args)
    if command == "project":
        if not args:
            return run_wrapper("project", ["status"])
        if args and args[0] == "bootstrap":
            raise RuntimeError("nap project bootstrap was removed; use nap project create")
        return run_wrapper("project", args)
    if command == "create":
        raise RuntimeError("top-level nap create was removed; use nap project create")
    if command == "agent":
        return run_wrapper("agent", args)
    if command == "auth":
        return run_wrapper("auth", args)
    if command == "feedback":
        return run_wrapper("feedback", args)
    if command == "where":
        state = state_dir_status()
        print_json(
            {
                "bin": shutil.which("nap") or str(BIN_DIR / "nap"),
                "install_dir": str(INSTALL_DIR),
                "wrapper": str(INSTALL_DIR / "napseer_mcp_server.py"),
                "cwd": str(pathlib.Path.cwd()),
                "cwd_auth_path": state["active_auth_path"],
                "cwd_auth_configured": pathlib.Path(state["active_auth_path"]).exists(),
                **state,
                "path_warning": path_warning(),
                "status": "ok",
                "message": f"Napseer install and cwd paths resolved. {state['state_dir_message']}",
            }
        )
        return 0
    if command in {"help", "-h", "--help"}:
        print("Usage: nap [auth|project|gateway|agent|feedback|status|update|where|install]")
        print("  auth        Auth commands: login-local, status.")
        print("              Example: nap auth login-local")
        print("  project     Project commands: create, status, encryption, encrypted, plaintext.")
        print("              Examples: nap project create --encrypted, nap project encrypted, nap project plaintext")
        print("              Project encryption uses a project passphrase; use --passphrase or NAPSEER_PROJECT_PASSPHRASE for automation.")
        print("              Equivalent explicit form: nap project encryption set encrypted|plaintext")
        print("  gateway     Gateway commands: start, stop, configure, setup, unlock, lock, restart, kill, logs, status, vault.")
        print("              Start auto-selects a local port; use --port to pin one.")
        print("              Examples: nap gateway start, nap gateway vault, nap gateway vault rotate-secret --kind chat")
        print("  agent       Agent commands: list, workspaces, create, show, edit.")
        print("              list shows registered project agents; workspaces shows /agents folders.")
        print("  feedback    Feedback commands: list, global, status, resolve.")
        print("              Examples: nap feedback list, nap feedback global --all, nap feedback resolve <id> --notes TEXT")
        print("  status      Show cwd Napseer configuration and next setup commands.")
        print("  update      Refresh the globally installed nap command and wrapper.")
        print("  where       Show installed nap paths and cwd auth location.")
        print("  install     Reinstall/repair nap from the hosted script directory.")
        return 0
    raise RuntimeError(f"unknown command: {command}")


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv))
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, indent=2), file=sys.stderr)
        raise SystemExit(1)
