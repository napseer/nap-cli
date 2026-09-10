"""Real workers and synthetic HTTP provider: user sessions across repositories."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "resources/scripts/napseer_mcp_server.py"
sys.path.insert(0, str(SCRIPT.parent))
from napseer_credentials import CredentialStore, atomic_json, read_json


@pytest.fixture
def provider():
    state = {"refreshes": 0, "requests": 0, "failure": None, "revoked": False}
    mutex = threading.Lock()
    state["refresh_started"] = threading.Event()
    state["refresh_release"] = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def reply(self, status, value):
            data = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            with mutex:
                state["requests"] += 1
            if self.headers.get("Authorization") == "Bearer fresh-access" and not state["revoked"]:
                self.reply(200, {"ok": True})
            else:
                self.reply(401, {"error": {"code": "unauthorized"}})

        def do_POST(self):
            form = parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
            if self.path == "/v1/oauth/revoke":
                state["revoked"] = True
                return self.reply(200, {})
            assert self.path == "/v1/oauth/token", "no enrollment fallback is allowed"
            if state["failure"] == "transport":
                return self.reply(503, {"error": {"code": "unavailable"}})
            if state["failure"] == "malformed":
                return self.reply(200, {"access_token": "incomplete"})
            if form.get("refresh_token") != ["old-refresh"] or state["refreshes"]:
                return self.reply(401, {"error": {"code": "invalid_grant"}})
            if state.get("hold_refresh"):
                state["refresh_started"].set()
                assert state["refresh_release"].wait(5)
            with mutex:
                state["refreshes"] += 1
            self.reply(200, {"access_token": "fresh-access", "refresh_token": "fresh-refresh",
                "expires_in": 3600, "refresh_expires_at": "2099-01-01T00:00:00Z"})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def seed(root, url):
    root.mkdir(exist_ok=True)
    store = CredentialStore(root / ".napseer")
    store.login({"base_url": url, "token": "old-access", "refresh_token": "old-refresh",
                 "account_mode": "operator_account", "oauth_client_id": "nap-cli"})
    return store


def worker(root, expression):
    code = "import importlib.util, json, sys\n" + (
        "spec = importlib.util.spec_from_file_location('worker', sys.argv[1])\n"
        "m = importlib.util.module_from_spec(spec)\nspec.loader.exec_module(m)\n"
        "try:\n    result = " + expression + "\n    print(json.dumps({'ok': True, 'result': result}))\n"
        "except Exception as exc:\n    print(json.dumps({'ok': False, 'code': getattr(exc, 'code', type(exc).__name__)}))\n"
    )
    env = dict(os.environ, NAPSEER_PROJECT_ROOT=str(root), NAPSEER_TELEMETRY="0")
    for key in ("NAPSEER_AUTH_FILE", "NAPSEER_BASE_URL", "NAPSEER_PROJECT_ID"):
        env.pop(key, None)
    return subprocess.Popen([sys.executable, "-c", code, str(SCRIPT)], cwd=root, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def outcome(process):
    out, _err = process.communicate(timeout=15)
    assert process.returncode == 0, "worker must exit without exposing credential state"
    return json.loads(out)


def test_shared_login_refreshes_once_across_real_processes_and_survives_restart(tmp_path, provider):
    url, state = provider
    first, second = tmp_path / "first", tmp_path / "second"
    store = seed(first, url)
    second.mkdir()
    processes = [worker(root, "m.request_json('GET', '/v1/projects')") for root in [first, second]]
    assert all(outcome(p)["ok"] for p in processes)
    assert state["refreshes"] == 1
    assert outcome(worker(second, "m.request_json('GET', '/v1/projects')"))["ok"]
    assert state["refreshes"] == 1
    assert store.user.stat().st_mode & 0o777 == 0o600
    assert not (first / ".napseer/auth.json").exists()
    assert not (second / ".napseer/auth.json").exists()


@pytest.mark.parametrize("failure", ["transport", "malformed"])
def test_refresh_failure_preserves_credentials_and_recovers(tmp_path, provider, failure):
    url, state = provider
    store = seed(tmp_path, url)
    before = store.user.read_bytes()
    state["failure"] = failure
    assert not outcome(worker(tmp_path, "m.request_json('GET', '/v1/projects')"))["ok"]
    assert store.user.read_bytes() == before
    state["failure"] = None
    assert outcome(worker(tmp_path, "m.request_json('GET', '/v1/projects')"))["ok"]


def test_project_override_does_not_fall_back_to_user_authority(tmp_path, provider):
    url, state = provider
    store = seed(tmp_path, url)
    store.login({"base_url": url, "token": "npk_expired", "account_mode": "api_key"}, project=True)
    result = outcome(worker(tmp_path, "m.request_json('GET', '/v1/projects')"))
    assert not result["ok"]
    assert state["refreshes"] == 0
    assert read_json(store.user)["token"] == "old-access"
    store.logout(project=True)
    assert not outcome(worker(tmp_path, "m.request_json('GET', '/v1/projects')"))["ok"]
    assert state["refreshes"] == 0


def test_unconfigured_agent_never_enrolls_or_creates_project(tmp_path):
    result = outcome(worker(tmp_path, "m.resolve_project_id({})"))
    assert result == {"ok": False, "code": "auth_required"}
    assert not (tmp_path / ".napseer").exists()


def test_logout_revokes_and_next_worker_observes_tombstone(tmp_path, provider):
    url, state = provider
    store = seed(tmp_path, url)
    assert outcome(worker(tmp_path, "m.operator_logout({})"))["ok"]
    assert state["revoked"]
    assert read_json(store.user)["logged_out"]
    assert not outcome(worker(tmp_path, "m.request_json('GET', '/v1/projects')"))["ok"]
    assert state["requests"] == 0


def test_global_credentials_keep_repository_selections_independent(tmp_path, provider):
    url, _ = provider
    first = tmp_path / "first"
    store = seed(first, url)
    other = CredentialStore(tmp_path / "second/.napseer")
    store.write({**store.read(), "project_id": "first"})
    other.write({**other.read(), "project_id": "second"})
    assert "project_id" not in read_json(store.user)
    assert store.read()["project_id"] == "first"
    assert other.read()["project_id"] == "second"


def test_logout_waits_for_inflight_rotation_and_cannot_be_undone(tmp_path, provider):
    url,state=provider
    store=seed(tmp_path,url)
    state["hold_refresh"]=True
    refresh=worker(tmp_path,"m.request_json('GET','/v1/projects')")
    assert state["refresh_started"].wait(5)
    logout=worker(tmp_path,"m.operator_logout({})")
    state["refresh_release"].set()
    outcome(refresh)
    assert outcome(logout)["ok"]
    assert state["revoked"]
    assert read_json(store.user).get("logged_out")
    assert not outcome(worker(tmp_path,"m.request_json('GET','/v1/projects')"))["ok"]


def test_refresh_pins_credential_source_when_override_appears(tmp_path, provider):
    url,state=provider
    store=seed(tmp_path,url)
    state["hold_refresh"]=True
    refresh=worker(tmp_path,"m.request_json('GET','/v1/projects')")
    assert state["refresh_started"].wait(5)
    override={"base_url":url,"token":"npk_project_test","account_mode":"api_key"}
    store.login(override,project=True)
    state["refresh_release"].set()
    assert not outcome(refresh)["ok"], "retry observes the explicit override"
    assert read_json(store.project)==override
    assert read_json(store.user)["token"]=="fresh-access"


def test_legacy_migration_preserves_authority_and_general_selection_is_explicit(tmp_path,provider):
    url,_=provider
    store=seed(tmp_path,url)
    legacy={"base_url":url,"token":"legacy-project","account_mode":"operator_project","project_id":"test-project"}
    atomic_json(store.legacy,legacy)
    assert store.selected()[1]=="legacy_project"
    store.migrate()
    assert not store.legacy.exists()
    assert store.read()["token"]=="legacy-project"
    store.use_user()
    assert store.read()["token"]=="old-access"
    assert read_json(store.project)==legacy


def test_failed_login_initialization_releases_actual_loopback_socket(tmp_path,monkeypatch):
    import socket
    spec=importlib.util.spec_from_file_location("cleanup_worker",SCRIPT)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    reserved=socket.socket();reserved.bind(("127.0.0.1",0));port=reserved.getsockname()[1];reserved.close()
    monkeypatch.setattr(module,"request_json",lambda *a,**k: (_ for _ in ()).throw(RuntimeError("synthetic unavailable")))
    with pytest.raises(RuntimeError):
        module.oauth_loopback_authorize({"scope":"test","flow":"operator_login","port":port,"open_browser":False})
    probe=socket.socket()
    try: probe.bind(("127.0.0.1",port))
    finally: probe.close()


def test_native_login_uses_requested_service_pkce_callback_and_shared_store(tmp_path,monkeypatch):
    import base64
    import hashlib
    import urllib.request
    from urllib.parse import urlsplit, urlencode
    state={}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*_args): pass
        def do_POST(self):
            raw=self.rfile.read(int(self.headers['Content-Length']))
            if self.path.endswith('/authorizations'):
                state['authorization']=json.loads(raw)
                payload={"authorization_url":"http://synthetic-browser.invalid/consent"}
            else:
                form=parse_qs(raw.decode());state['exchanged']=True
                expected=base64.urlsafe_b64encode(hashlib.sha256(form['code_verifier'][0].encode()).digest()).decode().rstrip('=')
                assert expected==state['authorization']['code_challenge']
                assert form['code']==['synthetic-one-time-code']
                payload={"access_token":"synthetic-native-access","refresh_token":"synthetic-native-refresh","expires_in":3600}
            body=json.dumps(payload).encode();self.send_response(200);self.end_headers();self.wfile.write(body)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    spec=importlib.util.spec_from_file_location('native_login_worker',SCRIPT)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    monkeypatch.setattr(module,'AUTH_DIR',tmp_path/'.napseer')
    monkeypatch.setattr(module,'AUTH_PATH',tmp_path/'.napseer/auth.json')
    monkeypatch.setattr(module,'BASE_URL','http://127.0.0.1:1')
    store = CredentialStore(tmp_path/'.napseer')
    store.user.parent.mkdir(parents=True,exist_ok=True)
    store.user.write_text('{broken synthetic state')
    def consent(_url):
        grant=state['authorization']
        callback=grant['redirect_uri']+'?'+urlencode({'code':'synthetic-one-time-code','state':grant['state']})
        with urllib.request.urlopen(callback,timeout=3) as response: assert response.status==200
        return True
    monkeypatch.setattr(module.webbrowser,'open',consent)
    try:
        result=module.operator_account_login({'base_url':f'http://127.0.0.1:{server.server_port}','timeout_seconds':5})
        assert result['status']=='authenticated';assert state['exchanged']
        store=CredentialStore(tmp_path/'.napseer')
        assert store.user.exists();assert not store.legacy.exists()
        assert read_json(store.user)['base_url']==f'http://127.0.0.1:{server.server_port}'
    finally:server.shutdown();server.server_close();thread.join(timeout=2)


def test_corrupt_credentials_allow_help_but_never_authenticated_fallback(tmp_path):
    store=CredentialStore(tmp_path/'.napseer')
    store.user.parent.mkdir(parents=True,exist_ok=True)
    store.user.write_text('{broken synthetic state')
    result=outcome(worker(tmp_path,"m.request_json('GET','/v1/projects')"))
    assert result['code']=='CredentialError'
    assert store.user.read_text()=='{broken synthetic state'
    env=dict(os.environ,NAPSEER_PROJECT_ROOT=str(tmp_path))
    help_result=subprocess.run([sys.executable,str(SCRIPT),'auth','--help'],cwd=tmp_path,env=env,capture_output=True,text=True,timeout=10)
    assert help_result.returncode==0
    assert 'nap auth login' in help_result.stdout
