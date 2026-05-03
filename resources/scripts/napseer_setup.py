#!/usr/bin/env python3
"""One-command Napseer project setup.

Run from a project root:
  python3 napseer_setup.py

What it does:
  1. Creates or reuses the active local state directory
  2. Creates .gitignore inside it so local secrets are not committed
  3. Generates id_ed25519 there if missing
  4. Enrolls this project copy in Napseer
  5. Creates or reuses a Napseer project
  6. Writes ./.napseer/auth.json for local MCP wrappers

Optional environment:
  NAPSEER_BASE_URL=https://api.napseer.com
  NAPSEER_PROJECT_SLUG=<slug>
  NAPSEER_PROJECT_NAME=<name>
  NAPSEER_WORKER_NAME=<worker name>
"""

import json
import getpass
import base64
import os
import pathlib
import queue
import runpy
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


ROOT = pathlib.Path.cwd()


def local_state_dir():
    preferred = ROOT / ".napseer"
    legacy = ROOT / "napseer"
    if preferred.exists() or not legacy.exists():
        return preferred
    return legacy


NAPSEER_DIR = local_state_dir()
KEY_PATH = NAPSEER_DIR / "id_ed25519"
AUTH_PATH = NAPSEER_DIR / "auth.json"
VAULT_PATH = NAPSEER_DIR / "vault.json"
BASE_URL = os.environ.get("NAPSEER_BASE_URL", "https://api.napseer.com").rstrip("/")


def post_json(path, payload, token=None, idempotency_key=None):
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json", "User-Agent": "napseer-setup-python/0.1"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    request = urllib.request.Request(f"{BASE_URL}{path}", data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        raise RuntimeError(f"POST {path} failed: HTTP {exc.code}: {body}") from exc


def put_json(path, payload, token=None):
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json", "User-Agent": "napseer-setup-python/0.1"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(f"{BASE_URL}{path}", data=data, headers=headers, method="PUT")
    try:
        with urllib.request.urlopen(request) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        raise RuntimeError(f"PUT {path} failed: HTTP {exc.code}: {body}") from exc


def get_json(path):
    request = urllib.request.Request(
        f"{BASE_URL}{path}",
        headers={"User-Agent": "napseer-setup-python/0.1"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        raise RuntimeError(f"GET {path} failed: HTTP {exc.code}: {body}") from exc


def ensure_local_files():
    NAPSEER_DIR.mkdir(exist_ok=True)
    chmod_best_effort(NAPSEER_DIR, 0o700)
    gitignore = NAPSEER_DIR / ".gitignore"
    gitignore.write_text("*\n!.gitignore\n", encoding="utf-8")
    if not KEY_PATH.exists():
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(KEY_PATH)],
            check=True,
        )
    chmod_best_effort(KEY_PATH, 0o600)
    chmod_best_effort(pathlib.Path(str(KEY_PATH) + ".pub"), 0o644)


def read_master_passphrase():
    value = os.environ.get("NAPSEER_MASTER_PASSPHRASE")
    if value:
        return value
    if not sys.stdin.isatty():
        raise RuntimeError("NAPSEER_MASTER_PASSPHRASE is required in non-interactive mode")
    first = getpass.getpass("Create gateway master passphrase: ")
    second = getpass.getpass("Confirm gateway master passphrase: ")
    if first != second:
        raise RuntimeError("gateway master passphrases did not match")
    return first


def load_gateway_vault_helpers():
    local_wrapper = pathlib.Path(__file__).resolve().with_name("napseer_mcp_server.py")
    local_spake2 = pathlib.Path(__file__).resolve().with_name("napseer_spake2.py")
    namespace = None
    if not local_spake2.exists():
        remote_spake2 = get_json("/v1/scripts/napseer_spake2.py")
        spake2_content = remote_spake2.get("content")
        if isinstance(spake2_content, str) and spake2_content.strip():
            local_spake2.write_text(spake2_content, encoding="utf-8")
    if local_wrapper.exists():
        namespace = runpy.run_path(str(local_wrapper))
    else:
        remote = get_json("/v1/scripts/napseer_mcp_server.py")
        content = remote.get("content")
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("could not load gateway vault helper")
        namespace = {"__file__": str(ROOT / "napseer_mcp_server.py"), "__name__": "napseer_mcp_server_setup_helper"}
        exec(compile(content, "napseer_mcp_server.py", "exec"), namespace)
    for name in ("write_vault", "write_public_auth"):
        if name not in namespace:
            raise RuntimeError(f"gateway vault helper is missing {name}")
    return namespace


def write_gateway_vault(passphrase, public_auth, secret_auth):
    helpers = load_gateway_vault_helpers()
    helpers["write_vault"](
        passphrase,
        {
            "version": 1,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "secrets": {key: value for key, value in secret_auth.items() if value is not None},
        },
    )
    helpers["write_public_auth"]({**public_auth, "gateway_auth_required": True, "vault_path": str(VAULT_PATH)})


def remove_plain_private_key():
    try:
        if KEY_PATH.exists():
            KEY_PATH.unlink()
    except OSError as exc:
        raise RuntimeError(f"failed to remove plaintext private key at {KEY_PATH}: {exc}") from exc


def chmod_best_effort(path, mode):
    try:
        path.chmod(mode)
    except OSError:
        # Windows and some mounted filesystems do not support POSIX modes.
        pass


def sign_text(namespace, key_path, text):
    with tempfile.TemporaryDirectory() as tmp:
        data_path = pathlib.Path(tmp) / "data.txt"
        data_path.write_text(text, encoding="utf-8")
        subprocess.run(
            ["ssh-keygen", "-Y", "sign", "-q", "-n", namespace, "-f", str(key_path), str(data_path)],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        return pathlib.Path(str(data_path) + ".sig").read_text(encoding="utf-8")


def default_slug():
    raw = ROOT.name.lower()
    slug = "".join(char if char.isalnum() else "-" for char in raw).strip("-")
    return slug or "napseer-project"


def choose_account_mode():
    mode = os.environ.get("NAPSEER_ACCOUNT_MODE", "").strip().lower()
    if mode in {"anonymous", "login"}:
        return mode
    if not sys.stdin.isatty():
        return "anonymous"

    print("Napseer account mode:")
    print("  1. Continue anonymous. Fastest, but recovery depends on local SSH keys.")
    print("  2. Login/claim account. Lets you recover and rotate SSH keys after web authentication.")
    choice = input("Choose [1/2] (default 1): ").strip()
    return "login" if choice == "2" else "anonymous"


def start_claim_receiver():
    result_queue = queue.Queue(maxsize=1)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            return

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != "/napseer/claim-callback":
                self.send_response(404)
                self.end_headers()
                return

            params = urllib.parse.parse_qs(parsed.query)
            result = {
                "status": params.get("status", [""])[0],
                "account_id": params.get("account_id", [""])[0],
                "claim_result_token": params.get("claim_result_token", [""])[0],
            }
            try:
                result_queue.put_nowait(result)
            except queue.Full:
                pass

            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                b"<html><body><h1>Napseer account connected.</h1><p>You can return to the terminal.</p></body></html>"
            )

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    return {
        "server": server,
        "queue": result_queue,
        "return_url": f"http://127.0.0.1:{port}/napseer/claim-callback",
    }


def wait_for_claim_result(receiver, timeout_seconds):
    try:
        result = receiver["queue"].get(timeout=timeout_seconds)
    except queue.Empty:
        return None
    finally:
        receiver["server"].shutdown()

    token = result.get("claim_result_token")
    if not token:
        return result

    verified = get_json(f"/v1/account/claim-results/{urllib.parse.quote(token)}")
    result["verified"] = verified
    return result


def main():
    ensure_local_files()
    master_passphrase = read_master_passphrase()
    public_key = pathlib.Path(str(KEY_PATH) + ".pub").read_text(encoding="utf-8").strip()
    private_key = KEY_PATH.read_text(encoding="utf-8")
    project_slug = os.environ.get("NAPSEER_PROJECT_SLUG", default_slug())
    project_name = os.environ.get("NAPSEER_PROJECT_NAME", ROOT.name or "Napseer Project")
    worker_name = os.environ.get("NAPSEER_WORKER_NAME", f"{project_slug}-agent")
    device_fingerprint = os.environ.get("NAPSEER_DEVICE_FINGERPRINT", socket.gethostname())

    challenge = post_json(
        "/v1/enrollment/challenges",
        {
            "public_key": public_key,
            "worker_name": worker_name,
            "device_fingerprint": device_fingerprint,
            "root_path": str(ROOT),
            "worker_capabilities": {"local_mcp": True, "gateway": True, "setup_script": "napseer_setup.py"},
        },
    )
    signature = sign_text("napseer", KEY_PATH, challenge["challenge_text"])
    verified = post_json(
        "/v1/enrollment/verify",
        {"challenge_id": challenge["challenge_id"], "signature": signature},
    )
    token = verified["token"]["access_token"]

    project = post_json(
        "/v1/projects",
        {
            "slug": project_slug,
            "name": project_name,
            "description": f"Project initialized from {ROOT}",
        },
        token=token,
        idempotency_key=f"napseer-setup-{project_slug}",
    )
    signing_key = put_json(
        f"/v1/projects/{project['id']}/signing-key",
        {"public_key": public_key, "label": "local-project-key"},
        token=token,
    )
    account_mode = choose_account_mode()
    account_claim = None
    claim_receiver = None
    if account_mode == "login":
        configured_return_url = os.environ.get("NAPSEER_CLAIM_RETURN_URL")
        claim_return_url = configured_return_url
        if not claim_return_url and sys.stdin.isatty():
            claim_receiver = start_claim_receiver()
            claim_return_url = claim_receiver["return_url"]
        account_claim = post_json(
            "/v1/account/claim-links",
            {"return_url": claim_return_url},
            token=token,
        )

    public_auth = {
        "base_url": BASE_URL,
        "project_id": project["id"],
        "project_slug": project["slug"],
        "project_name": project["name"],
        "project_signing_key_fingerprint": signing_key["fingerprint"],
        "worker_id": verified["worker"]["id"],
        "agent_id": verified["worker"]["agent_id"],
        "worker_name": worker_name,
        "device_fingerprint": device_fingerprint,
        "root_path": str(ROOT),
        "worker_capabilities": {"local_mcp": True, "gateway": True, "setup_script": "napseer_setup.py"},
        "account_mode": "claim_pending" if account_claim else "anonymous",
    }
    secret_auth = {
        "token": token,
        "token_expires_at": verified["token"].get("expires_at"),
        "ssh_private_key": private_key,
        "ssh_public_key": public_key,
        "gateway_encryption_key": base64.b64encode(secrets.token_bytes(32)).decode("ascii"),
    }
    if account_claim:
        secret_auth["account_claim_url"] = account_claim["claim_url"]
        secret_auth["account_claim_expires_at"] = account_claim["expires_at"]
    write_gateway_vault(master_passphrase, public_auth, secret_auth)
    remove_plain_private_key()

    output = {
        "status": "ok",
        "auth_path": str(AUTH_PATH),
        "vault_path": str(VAULT_PATH),
        "project_id": project["id"],
        "agent_id": verified["worker"]["agent_id"],
        "project_signing_key_fingerprint": signing_key["fingerprint"],
        "next": [
            "Run the local MCP wrapper from this project root.",
            "python3 napseer_mcp_server.py",
        ],
    }
    if account_claim:
        output["account_claim"] = {
            "claim_url": account_claim["claim_url"],
            "expires_at": account_claim["expires_at"],
            "next": "Open this URL in a browser to login and attach this anonymous account.",
        }
    print(json.dumps(output, indent=2))

    if account_claim and claim_receiver:
        print("Opening browser for Napseer account claim...", file=sys.stderr)
        webbrowser.open(account_claim["claim_url"])
        timeout = int(os.environ.get("NAPSEER_CLAIM_WAIT_SECONDS", "180"))
        claim_result = wait_for_claim_result(claim_receiver, timeout)
        if claim_result:
            public_auth["account_mode"] = "claimed"
            public_auth["claimed_account_id"] = claim_result.get("account_id")
            public_auth["claimed_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            secret_auth["claim_result"] = claim_result.get("verified", claim_result)
            write_gateway_vault(master_passphrase, public_auth, secret_auth)
            print(json.dumps({"status": "claimed", "account_id": public_auth["claimed_account_id"]}, indent=2))
        else:
            print(
                json.dumps(
                    {
                        "status": "claim_wait_timeout",
                        "claim_url": account_claim["claim_url"],
                        "next": "Open the claim URL manually; the local receiver has stopped.",
                    },
                    indent=2,
                )
            )


if __name__ == "__main__":
    main()
