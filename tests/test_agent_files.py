"""Public worker/CLI entry points against synthetic binary HTTP storage."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import uuid
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs

import pytest
from test_user_credentials import worker, outcome, SCRIPT
from napseer_credentials import CredentialStore, atomic_json

PROJECT="11111111-1111-4111-8111-111111111111"
NODE="22222222-2222-4222-8222-222222222222"

@pytest.fixture
def storage(tmp_path):
    state={"files":{},"corrupt":False,"keys":[],"projects":[]}
    node={"id":NODE,"name":"Guide","full_path":"/services/guide","type":"documentation","updated_at":"2026-09-09T00:00:00Z","content_text":"Recovery procedure","metadata":{},"links":[]}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*_args): pass
        def reply(self,status,value,binary=False):
            body=value if binary else json.dumps(value).encode()
            self.send_response(status);self.send_header("Content-Length",str(len(body)));self.end_headers();self.wfile.write(body)
        def do_GET(self):
            path=urlsplit(self.path).path
            if path=='/v1/projects':return self.reply(200,{'items':state['projects']})
            if path=='/v1/api-keys':return self.reply(200,{'items':state['keys']})
            if path.endswith('/nodes'): return self.reply(200,{"items":[node]})
            if path.endswith('/nodes/'+NODE):return self.reply(200,node)
            if path.endswith('/files'): return self.reply(200,{"items":[entry[0] for entry in state["files"].values()]})
            identity=path.split('/')[-2] if path.endswith('/metadata') else path.split('/')[-1]
            entry=state["files"].get(identity)
            if not entry:return self.reply(404,{"error":{"code":"not_found"}})
            if path.endswith('/metadata'):return self.reply(200,entry[0])
            return self.reply(200,b"corrupted" if state["corrupt"] else entry[1],True)
        def do_POST(self):
            if self.path=='/v1/projects':
                payload=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                project={**payload,'id':str(uuid.uuid4()),'encryption_state':'standard'}
                state['projects'].append(project);return self.reply(200,project)
            if self.path=='/v1/api-keys':
                payload=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                key={**payload,'id':str(uuid.uuid4())};state['keys'].append(key)
                return self.reply(200,{**key,'api_key':'npk_'+uuid.uuid4().hex})
            query=parse_qs(urlsplit(self.path).query)
            body=self.rfile.read(int(self.headers['Content-Length']))
            identity=query['id'][0]
            info={"id":identity,"name":query['name'][0],"node_id":query.get('node_id',[None])[0],"size_bytes":len(body),"sha256":hashlib.sha256(body).hexdigest()}
            state['files'][identity]=(info,body)
            return self.reply(200,info)
        def do_DELETE(self):
            identity=urlsplit(self.path).path.split('/')[-1];state['files'].pop(identity,None)
            return self.reply(200,{"archived":True})
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    store=CredentialStore(tmp_path/'.napseer')
    store.login({"base_url":f"http://127.0.0.1:{server.server_port}","token":"synthetic-access","account_mode":"operator_account"})
    atomic_json(store.selection,{"project_id":PROJECT})
    try:yield state
    finally:server.shutdown();server.server_close();thread.join(timeout=2)


def test_agent_upload_retries_download_and_export_preserve_binary_and_references(tmp_path,storage):
    body=bytes([0,255,128,13,10,65]);source=tmp_path/'proof.bin';source.write_bytes(body)
    args={"local_path":str(source),"node_id":NODE}
    first=outcome(worker(tmp_path,f"m.call_tool_impl('nap_file_upload',{args!r})"))
    assert first['ok'],first.get('code')
    identity=first['result']['id']
    again=outcome(worker(tmp_path,f"m.call_tool_impl('nap_file_upload',{args!r})"))
    assert again['result']['id']==identity
    assert len(storage['files'])==1
    destination=tmp_path/'download.bin'
    result=outcome(worker(tmp_path,f"m.call_tool_impl('nap_file_download',{{'id':{identity!r},'local_path':{str(destination)!r}}})"))
    assert result['ok'];assert destination.read_bytes()==body
    rejected=outcome(worker(tmp_path,f"m.call_tool_impl('nap_file_download',{{'id':{identity!r},'local_path':{str(source)!r}}})"))
    assert rejected['code']=='destination_unavailable';assert source.read_bytes()==body
    target=tmp_path/'export.zip'
    env=dict(os.environ,NAPSEER_PROJECT_ROOT=str(tmp_path),NAPSEER_TELEMETRY='0')
    for name in ['NAPSEER_AUTH_FILE','NAPSEER_BASE_URL','NAPSEER_PROJECT_ID']:env.pop(name,None)
    exported=subprocess.run([sys.executable,str(SCRIPT),'export','--path',str(target)],cwd=tmp_path,env=env,capture_output=True,text=True,timeout=15)
    assert exported.returncode==0
    assert json.loads(exported.stdout)['status']=='exported'
    with zipfile.ZipFile(target) as archive:
        manifest=json.loads(archive.read('manifest.json'))
        assert manifest['files'][0]['node_id']==NODE
        assert archive.read('files/'+identity)==body
        assert json.loads(archive.read('nodes/'+NODE+'.json'))['content_text']=='Recovery procedure'
        assert not any('auth' in name for name in archive.namelist())
    storage['corrupt']=True
    rejected=outcome(worker(tmp_path,f"m.export_project({{'local_path':{str(tmp_path/'broken.zip')!r}}})"))
    assert rejected['code']=='file_integrity_failed'
    assert not (tmp_path/'broken.zip').exists()
    assert not list(tmp_path.glob('.napseer-export-*'))
    assert outcome(worker(tmp_path,f"m.call_tool_impl('nap_file_archive',{{'id':{identity!r}}})"))['ok']
    assert not storage['files']


def test_cli_key_issuance_writes_private_credential_without_printing_it(tmp_path,storage):
    env=dict(os.environ,NAPSEER_PROJECT_ROOT=str(tmp_path),NAPSEER_TELEMETRY='0')
    for name in ['NAPSEER_AUTH_FILE','NAPSEER_BASE_URL','NAPSEER_PROJECT_ID']:env.pop(name,None)
    created=subprocess.run([sys.executable,str(SCRIPT),'auth','keys','create','--project-id',PROJECT,'--name','Test delegate'],cwd=tmp_path,env=env,capture_output=True,text=True,timeout=15)
    assert created.returncode==0
    assert 'npk_' not in created.stdout
    result=json.loads(created.stdout);path=Path(result['credential_file'])
    assert path.stat().st_mode & 0o777 == 0o600
    assert json.loads(path.read_text())['token'].startswith('npk_')
    assert not (tmp_path/'.napseer/auth.json').exists()


def test_two_agents_bootstrap_one_project_with_general_login(tmp_path,storage):
    store=CredentialStore(tmp_path/'.napseer');store.selection.unlink()
    first=worker(tmp_path,"m.resolve_project_id({})")
    second=worker(tmp_path,"m.resolve_project_id({})")
    one,two=outcome(first),outcome(second)
    assert one['ok'] and two['ok']
    assert one['result']==two['result']
    assert len(storage['projects'])==1
    assert json.loads(store.user.read_text())['account_mode']=='operator_account'
    assert 'project_id' not in json.loads(store.user.read_text())
    assert not store.legacy.exists()
    assert (tmp_path/'.napseer/project.json').exists()
