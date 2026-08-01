#!/usr/bin/env python3
"""Smoke-test local MCP memory node encryption behavior."""

import base64
import importlib.util
import pathlib
import sys
import tempfile


ACCOUNT_ID = "22222222-2222-2222-2222-222222222222"
PASSPHRASE = "correct horse battery staple"


def load_module():
    script_path = pathlib.Path(__file__).resolve().parents[1] / "resources" / "scripts" / "napseer_mcp_server.py"
    sys.path.insert(0, str(script_path.parent))
    spec = importlib.util.spec_from_file_location("napseer_mcp_server_memory_test", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def secret(version=3):
    return {
        "project_id": "project-1",
        "secret_kind": "memory",
        "version": version,
        "wrapping_epoch": 1,
        "bundle_version": version,
        "data_key_epoch": version,
        "secret_bytes": bytes(range(32)),
        "created_at": "2026-05-05T00:00:00Z",
        "last_rotated_at": "2026-05-05T00:00:00Z",
    }


def cache_secret(mod, project_id="project-1", version=3, active=True):
    item = secret(version=version)
    slot = (project_id, "memory", version, item["wrapping_epoch"], item["bundle_version"], item["data_key_epoch"])
    mod.MEMORY_SECRET_CACHE[slot] = item
    if active:
        mod.ACTIVE_MEMORY_SECRET_VERSIONS[project_id] = slot
    return slot


def wrapped_memory_response(mod, project_id, version, wrapping_epoch=1, bundle_version=None, data_key_epoch=None):
    bundle_version = version if bundle_version is None else bundle_version
    data_key_epoch = version if data_key_epoch is None else data_key_epoch
    plaintext = mod.generate_project_data_key_bundle(project_id, ACCOUNT_ID, data_key_epoch=data_key_epoch)
    plaintext["keys"]["memory"]["active_version"] = data_key_epoch
    plaintext["keys"]["memory"]["versions"][str(data_key_epoch)]["key_b64"] = base64.b64encode(bytes(range(32))).decode("ascii")
    wrapped = mod.wrapped_project_key_bundle_record(
        project_id,
        ACCOUNT_ID,
        "memory",
        plaintext,
        wrapping_epoch=wrapping_epoch,
        bundle_version=bundle_version,
        data_key_epoch=data_key_epoch,
    )
    return {
        "secret_kind": "memory",
        "version": version,
        "project_secret_version": version,
        "wrapping_epoch": wrapping_epoch,
        "bundle_version": bundle_version,
        "data_key_epoch": data_key_epoch,
        "wrapped_key_bundle": wrapped,
        "created_at": "2026-05-05T00:00:00Z",
        "last_rotated_at": "2026-05-05T00:00:00Z",
    }


def encrypted_node(mod, full_path="/notes/alpha", content="secret needle", metadata=None, version=3):
    active = secret(version=version)
    content_env = mod.encrypt_memory_bytes(
        "project-1",
        active,
        "node_content",
        content.encode("utf-8"),
        {"full_path": full_path},
        "text/plain; charset=utf-8",
    )
    metadata_env = None
    if metadata is not None:
        metadata_env = mod.encrypt_memory_bytes(
            "project-1",
            active,
            "node_metadata",
            mod.stable_json_bytes(metadata),
            {"full_path": full_path},
            "application/json; charset=utf-8",
        )
    return {
        "id": "node-1",
        "project_id": "project-1",
        "full_path": full_path,
        "folder_path": "/notes",
        "name": full_path.rsplit("/", 1)[-1],
        "type": "note",
        "tags": ["alpha"],
        "aliases": ["alias-alpha"],
        "links": [{"path": "/notes/ref", "relation": "references"}],
        "metadata": {},
        "content_text": "",
        "encryption_state": "encrypted",
        "project_secret_version": active["version"],
        "encrypted_content_envelope": content_env,
        "encrypted_metadata_envelope": metadata_env,
    }


def run():
    mod = load_module()
    test_state = tempfile.TemporaryDirectory()
    mod.AUTH_PATH = pathlib.Path(test_state.name) / "auth.json"
    mod.AUTH = {"account_id": ACCOUNT_ID, "token": "token", "token_expires_at": "later"}
    mod.PROJECT_VAULT_PASSPHRASE = PASSPHRASE
    mod.DEFAULT_PROJECT_ID = "project-1"
    mod.MEMORY_SECRET_CACHE.clear()
    mod.ACTIVE_MEMORY_SECRET_VERSIONS.clear()
    cache_secret(mod)
    real_active_memory_secret = mod.active_memory_secret
    real_get_node_by_id = mod.get_node_by_id

    envelope = mod.encrypt_memory_bytes(
        "project-1",
        secret(),
        "node_content",
        b"hello memory",
        {"full_path": "/notes/roundtrip"},
        "text/plain; charset=utf-8",
    )
    assert envelope["active_project_secret_version"] == 3
    assert envelope["key_id"] == "memory:v3:node_content"
    assert envelope["aad_subject"].startswith("encryption_id:")
    plaintext = mod.decrypt_memory_envelope("project-1", secret(), "node_content", envelope, {"full_path": "/notes/roundtrip"})
    assert plaintext == b"hello memory"
    renamed_plaintext = mod.decrypt_memory_envelope("project-1", secret(), "node_content", envelope, {"full_path": "/renamed/roundtrip"})
    assert renamed_plaintext == b"hello memory"

    legacy_envelope = dict(envelope)
    legacy_envelope.pop("aad_subject")
    try:
        mod.decrypt_memory_envelope("project-1", secret(), "node_content", legacy_envelope, {"full_path": "/renamed/roundtrip"})
    except RuntimeError:
        pass
    else:
        raise AssertionError("legacy full_path AAD envelopes must still fail closed when moved")

    mod.project_memory_encryption_active = lambda project_id: True
    mod.active_memory_secret = lambda project_id, force_refresh=False: secret()
    payload = mod.encrypt_node_payload_for_write(
        "project-1",
        {"folder_path": "/notes", "name": "alpha", "content_text": "classified", "metadata": {"private": "value"}},
    )
    assert payload["content_text"] == ""
    assert payload["metadata"] == {}
    assert payload["encryption_state"] == "encrypted"
    assert payload["project_secret_version"] == 3
    assert payload["encrypted_content_envelope"]["key_id"] == "memory:v3:node_content"
    assert payload["encrypted_metadata_envelope"]["key_id"] == "memory:v3:node_metadata"
    assert payload["encrypted_content_envelope"]["aad_subject"].startswith("encryption_id:")
    node = {
        "id": "node-1",
        "project_id": "project-1",
        "full_path": "/notes/alpha",
        "encryption_state": "encrypted",
        "encrypted_content_envelope": payload["encrypted_content_envelope"],
        "encrypted_metadata_envelope": payload["encrypted_metadata_envelope"],
        "content_text": "",
        "metadata": {},
    }
    decrypted = mod.decrypt_node_for_return(node, project_id="project-1")
    assert decrypted["content_text"] == "classified"
    assert decrypted["metadata"] == {"private": "value"}

    def fail_secret(project_id, force_refresh=False):
        raise RuntimeError("active memory secret is unavailable")

    mod.active_memory_secret = fail_secret
    try:
        mod.encrypt_node_payload_for_write("project-1", {"path": "/notes/fail", "content_text": "plaintext"})
    except RuntimeError:
        pass
    else:
        raise AssertionError("encrypted projects must fail closed when memory secret fetch fails")
    try:
        mod.get_node_by_id = lambda args: encrypted_node(mod, full_path="/notes/alpha")
        mod.guarded_node_patch({
            "node_id": "node-1",
            "precondition": {"revision": "r1", "read_fingerprint": "fp1"},
            "content_op": {"op": "replace_all", "content_text": "plaintext"},
        })
    except RuntimeError as exc:
        assert "cannot safely patch encrypted content or metadata" in str(exc)
    else:
        raise AssertionError("nap_node_patch content updates must fail closed for encrypted projects")

    write_fetches = []
    mod.active_memory_secret = real_active_memory_secret
    mod.MEMORY_SECRET_CACHE.clear()
    mod.ACTIVE_MEMORY_SECRET_VERSIONS.clear()
    mod.request_json = lambda method, path, payload=None, **kwargs: write_fetches.append((method, path)) or {
        **wrapped_memory_response(mod, "project-1", 6, wrapping_epoch=2, bundle_version=6, data_key_epoch=6)
    }
    write_payload = mod.encrypt_node_payload_for_write(
        "project-1",
        {"folder_path": "/notes", "name": "write-active", "content_text": "write secret"},
    )
    assert write_payload["project_secret_version"] == 6
    assert write_payload["wrapping_epoch"] == 2
    assert write_payload["bundle_version"] == 6
    assert write_payload["data_key_epoch"] == 6
    assert write_fetches == [("GET", "/v1/projects/project-1/vault/secrets/memory/active")]

    fetched = []
    mod.active_memory_secret = real_active_memory_secret
    mod.MEMORY_SECRET_CACHE.clear()
    mod.ACTIVE_MEMORY_SECRET_VERSIONS.clear()

    def memory_secret_response(method, path, payload=None, **kwargs):
        fetched.append((method, path))
        version = 5
        if path.endswith("/versions/4"):
            version = 4
        return wrapped_memory_response(mod, "project-1", version)

    mod.request_json = memory_secret_response
    active = mod.active_memory_secret("project-1")
    assert active["version"] == 5
    assert fetched == [("GET", "/v1/projects/project-1/vault/secrets/memory/active")]
    assert "project_secret_b64" not in str(mod.MEMORY_SECRET_CACHE)
    assert "wrapped_key_bundle" not in str(mod.MEMORY_SECRET_CACHE)
    assert set(mod.MEMORY_SECRET_CACHE) == {("project-1", "memory", 5, 1, 5, 5)}
    assert mod.memory_secret_for_read("project-1", 5)["version"] == 5
    old_node = encrypted_node(mod, full_path="/notes/old-version", content="old version plaintext", version=4)
    decrypted_old = mod.decrypt_node_for_return(old_node, project_id="project-1")
    assert decrypted_old["content_text"] == "old version plaintext"
    old = mod.memory_secret_for_read("project-1", 4)
    assert old["version"] == 4
    assert fetched[-1] == ("GET", "/v1/projects/project-1/vault/secrets/memory/versions/4")
    assert set(mod.MEMORY_SECRET_CACHE) == {("project-1", "memory", 5, 1, 5, 5), ("project-1", "memory", 4, 1, 4, 4)}
    assert mod.memory_secret_for_read("project-1", 4) is old
    assert fetched.count(("GET", "/v1/projects/project-1/vault/secrets/memory/versions/4")) == 1

    mod.MEMORY_SECRET_CACHE.clear()
    mod.ACTIVE_MEMORY_SECRET_VERSIONS.clear()
    cache_secret(mod)
    node = encrypted_node(mod, metadata={"private": "secret metadata"})
    mod.get_node_by_id = real_get_node_by_id
    mod.request_json = lambda method, path, payload=None, **kwargs: node
    read = mod.get_node_by_id({"node_id": "node-1"})
    assert read["content_text"] == "secret needle"
    assert read["metadata"] == {"private": "secret metadata"}

    moved = dict(node)
    moved["full_path"] = "/renamed/alpha"
    moved["folder_path"] = "/renamed"
    mod.request_json = lambda method, path, payload=None, **kwargs: {"items": [moved]}
    listed = mod.list_project_nodes({"limit": 10, "include_content": True})
    assert "content_text" not in listed["items"][0]
    assert listed["items"][0]["full_path"] == "/renamed/alpha"

    mod.request_json = lambda method, path, payload=None, **kwargs: {"items": [moved]}
    compact = mod.list_project_nodes({"limit": 10})
    assert compact["ok"] is True
    assert compact["view"] == "paths"
    assert "content_text" not in compact["items"][0]
    assert compact["items"][0]["full_path"] == "/renamed/alpha"
    full = mod.list_project_nodes({
        "limit": 10,
        "view": "full",
        "_allow_full": True,
    })
    assert full["items"][0]["content_text"] == "secret needle"

    plan_base = {
        "project_id": "project-1",
        "folder_path": "/plans",
        "name": "plan",
        "type": "note",
        "tags": ["plan"],
        "links": [],
        "content_text": "plan body",
        "encryption_state": "legacy_plaintext",
    }
    active_nodes = [
        {**plan_base, "id": "active", "full_path": "/plans/active", "metadata": {"status": "planned"}},
        {**plan_base, "id": "done", "full_path": "/plans/done", "metadata": {"status": "completed"}},
        {**plan_base, "id": "archived", "full_path": "/plans/archived", "metadata": {"status": "planned"}, "archived_at": "2026-05-06T00:00:00Z"},
    ]
    mod.request_json = lambda method, path, payload=None, **kwargs: {"items": active_nodes}
    active = mod.list_project_nodes({"folder_path": "/plans", "active_only": True})
    assert [item["full_path"] for item in active["items"]] == ["/plans/active"]
    status_filtered = mod.list_project_nodes({"folder_path": "/plans", "status": ["completed"], "view": "paths"})
    assert status_filtered["items"] == [{"full_path": "/plans/done", "type": "note", "status": "completed"}]

    def search_response(method, path, payload=None, **kwargs):
        assert "q=" in path
        return {"items": [dict(moved)]}

    mod.request_json = search_response
    found = mod.list_project_nodes({"q": "secret needle", "limit": 5})
    assert "query_analysis" not in found
    assert "diagnostics" not in found
    verbose_found = mod.list_project_nodes({"q": "secret needle", "limit": 5, "verbose": True})
    assert "query_analysis" in verbose_found
    assert "diagnostics" in verbose_found

    legacy_moved = encrypted_node(mod, full_path="/notes/legacy", content="old")
    legacy_moved["encrypted_content_envelope"].pop("aad_subject")
    legacy_moved["full_path"] = "/renamed/legacy"
    mod.request_json = lambda method, path, payload=None, **kwargs: legacy_moved
    try:
        mod.get_node_by_id({"node_id": "node-1"})
    except RuntimeError:
        pass
    else:
        raise AssertionError("legacy moved full_path-bound node must fail closed")

    mod.MEMORY_SECRET_CACHE.clear()
    mod.ACTIVE_MEMORY_SECRET_VERSIONS.clear()
    cache_secret(mod)
    mod.AUTH = {"token": "token", "token_expires_at": "later"}
    mod.load_auth = lambda: {"token": "token", "token_expires_at": "later"}
    mod.vault_exists = lambda: True
    cache_secret(mod, version=4, active=False)
    assert mod.MEMORY_SECRET_CACHE
    mod.clear_gateway_runtime_caches()
    assert mod.MEMORY_SECRET_CACHE == {}
    assert mod.ACTIVE_MEMORY_SECRET_VERSIONS == {}

    cache_secret(mod)
    mod.project_memory_encryption_active = lambda project_id: True
    mod.active_memory_secret = lambda project_id, force_refresh=False: secret()
    writes = []
    mod.resolve_project_id = lambda args: "project-1"
    mod.request_project_write = lambda method, path, body, project_id, purpose, *extra, **kwargs: writes.append(
        (method, path, body, project_id, purpose, extra, kwargs)
    ) or {
        "id": "node-2",
        "project_id": project_id,
        "full_path": body.get("folder_path", "/notes").rstrip("/") + "/" + body.get("name", "beta"),
        "folder_path": body.get("folder_path", "/notes"),
        "name": body.get("name", "beta"),
        "type": body.get("type", "note"),
        "metadata": {},
        "content_text": "",
        "encryption_state": "encrypted",
        "project_secret_version": body["project_secret_version"],
        "encrypted_content_envelope": body["encrypted_content_envelope"],
        "encrypted_metadata_envelope": body.get("encrypted_metadata_envelope"),
    }
    real_index_node = mod.index_node
    mod.index_node = lambda node: None
    mod.try_get_node_by_path = lambda path, allow_agent=False: None
    created = mod.upsert_node({"path": "/notes/beta", "content_text": "tee secret", "metadata": {"private": "tee"}})
    tee_body = writes[-1][2]
    assert created["ok"] is True
    assert created["created"] is True
    assert created["path"] == "/notes/beta"
    assert "content_text" not in created
    assert "metadata" not in created
    assert tee_body["content_text"] == ""
    assert tee_body["metadata"] == {}
    assert tee_body["wrapping_epoch"] == 1
    assert tee_body["bundle_version"] == 3
    assert tee_body["data_key_epoch"] == 3
    assert tee_body["encrypted_content_envelope"]["aad_subject"].startswith("encryption_id:")

    existing = encrypted_node(mod, full_path="/notes/beta", content="old")
    mod.get_node_by_id = lambda args: dict(existing)
    writes.clear()
    patched = mod.update_node_by_path({"node_id": "node-1", "content_text": "patch secret"})
    patch_body = writes[-1][2]
    assert patched["ok"] is True
    assert patched["updated"] is True
    assert patched["path"] == "/notes/beta"
    assert "content_text" not in patched
    assert patch_body["encrypted_content_envelope"]["aad_subject"] == "node_id:node-1"

    bulk_writes = []
    mod.acquire_project_lock = lambda args: {"id": "lock-1", "lease_token": "lease-1"}
    mod.lock_headers = lambda lock: {"X-Lock": lock["id"]}
    mod.release_project_lock = lambda args: None
    mod.try_get_node_by_path = lambda path, allow_agent=False: None
    mod.request_json = lambda method, path, payload=None, **kwargs: bulk_writes.append((method, path, payload, kwargs)) or {
        "id": "node-bulk",
        "project_id": "project-1",
        "full_path": payload.get("folder_path", "/bulk").rstrip("/") + "/" + payload.get("name", "one"),
        "folder_path": payload.get("folder_path", "/bulk"),
        "name": payload.get("name", "one"),
        "type": payload.get("type", "note"),
        "metadata": {},
        "content_text": "",
        "encryption_state": "encrypted",
        "project_secret_version": payload["project_secret_version"],
        "encrypted_content_envelope": payload["encrypted_content_envelope"],
        "encrypted_metadata_envelope": payload.get("encrypted_metadata_envelope"),
    }
    bulk = mod.bulk_upsert_nodes({"nodes": [{"path": "/bulk/one", "content_text": "bulk secret"}]})
    assert bulk["items"][0]["path"] == "/bulk/one"
    assert "node" not in bulk["items"][0]
    assert "content_text" not in bulk["items"][0]
    assert bulk_writes[0][2]["content_text"] == ""
    assert bulk_writes[0][2]["encrypted_content_envelope"]["aad_subject"].startswith("encryption_id:")

    vector_secret = {
        "project_id": "project-vector",
        "secret_kind": "memory",
        "version": 7,
        "wrapping_epoch": 1,
        "bundle_version": 7,
        "data_key_epoch": 7,
        "secret_bytes": bytes(range(1, 33)),
        "created_at": "2026-05-05T00:00:00Z",
        "last_rotated_at": "2026-05-05T00:00:00Z",
    }
    vector_envelope = mod.encrypt_memory_bytes(
        "project-vector",
        vector_secret,
        "node_content",
        b"cross-client memory vector",
        {
            "encryption_id": "vector-001",
            "nonce_b64": "AAECAwQFBgcICQoL",
        },
        "text/plain; charset=utf-8",
    )
    assert vector_envelope == {
        "schema_version": 1,
        "alg": "AES-GCM-256",
        "nonce_b64": "AAECAwQFBgcICQoL",
        "ciphertext_b64": "vmTnjdn7GdeB84+TKTBOHYHlPH/+2sqySX6yo/SfaxlBb0ocr61W2jjP",
        "payload_size_bytes": 42,
        "active_project_secret_version": 7,
        "key_id": "memory:v7:node_content",
        "content_type": "text/plain; charset=utf-8",
        "aad_subject": "encryption_id:vector-001",
        "aad_hash": "a2eedd97e999f0db7215dd602a1426d841845355b95383b8aaec3473a03713ca",
        "ciphertext_sha256": "ae8036bf65d5ba48774517d6b387e5f55db703f53dab741ad566bb8618c7e467",
    }
    assert mod.decrypt_memory_envelope(
        "project-vector",
        vector_secret,
        "node_content",
        vector_envelope,
        {"full_path": "/renamed/vector"},
    ) == b"cross-client memory vector"

    with tempfile.TemporaryDirectory() as tmp:
        state_dir = pathlib.Path(tmp)
        mod.AUTH_DIR = state_dir
        mod.INDEX_PATH = state_dir / "index.sqlite"
        mod.INDEX_LOCK_PATH = state_dir / "index.lock"
        mod.index_node = real_index_node
        indexed = dict(read)
        indexed["content_text"] = "secret needle"
        indexed["metadata"] = {"private": "secret metadata"}
        mod.index_node(indexed)
        assert mod.search_local_index({"q": "alpha", "limit": 5})["items"]
        assert mod.search_local_index({"q": "needle", "limit": 5})["items"] == []
        assert mod.search_local_index({"q": "metadata", "limit": 5})["items"] == []

    print("ok: local MCP memory node encryption smoke passed")


if __name__ == "__main__":
    run()
