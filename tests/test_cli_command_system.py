import importlib.util
import hashlib
import json
import pathlib


SCRIPT_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "resources"
    / "scripts"
    / "nap_install.py"
)


def load_installer():
    spec = importlib.util.spec_from_file_location("nap_install_command_system_test", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fail_if_called(*_args, **_kwargs):
    raise AssertionError("help dispatched a side-effecting handler")


def test_root_help_exposes_only_canonical_commands(capsys):
    module = load_installer()

    assert module.main(["nap"]) == 0

    output = capsys.readouterr().out
    visible = [name for name, metadata in module.COMMAND_METADATA.items() if metadata["visible"]]
    assert visible == [name for name, _summary in module.CANONICAL_COMMANDS]
    assert len(visible) == 10
    for name in visible:
        assert f"  {name}" in output
    for hidden in ("chat", "plan", "lineage", "agent", "feedback", "where", "install"):
        assert f"  {hidden}" not in output


def test_help_is_resolved_before_mutating_dispatch(monkeypatch, capsys):
    module = load_installer()
    monkeypatch.setattr(module, "install_assets", fail_if_called)
    monkeypatch.setattr(module, "run_wrapper", fail_if_called)
    monkeypatch.setattr(module, "handle_gateway", fail_if_called)

    assert module.main(["nap", "update", "help"]) == 0
    assert module.main(["nap", "install", "--help"]) == 0
    assert module.main(["nap", "auth", "--help"]) == 0
    assert module.main(["nap", "project", "encryption", "--help"]) == 0
    assert module.main(["nap", "gateway", "vault", "help"]) == 0

    output = capsys.readouterr().out
    assert "Help never performs an update" in output
    assert "Use `nap mcp install`" in output
    assert "Usage: nap auth" in output
    assert "Usage: nap project" in output
    assert "Usage: nap gateway" in output


def test_help_topic_uses_same_canonical_text(capsys):
    module = load_installer()

    assert module.main(["nap", "help", "mcp"]) == 0

    assert "Usage: nap mcp [status|install|update|serve]" in capsys.readouterr().out


def test_init_and_auth_repair_normalize_to_existing_handlers(monkeypatch):
    module = load_installer()
    calls = []
    monkeypatch.setattr(
        module,
        "run_wrapper",
        lambda command, args: calls.append((command, args)) or 0,
    )

    assert module.main(["nap", "init", "--slug", "demo"]) == 0
    assert module.main(["nap", "auth", "repair"]) == 0

    assert calls == [
        ("project", ["init", "--slug", "demo"]),
        ("auth", ["repair"]),
    ]


def test_update_does_not_overwrite_repository_source(monkeypatch, capsys):
    module = load_installer()
    calls = []
    monkeypatch.setattr(
        module,
        "install_assets",
        lambda: calls.append("update") or {"status": "installed"},
    )

    assert module.main(["nap", "update"]) == 0

    assert calls == ["update"]
    assert json.loads(capsys.readouterr().out)["status"] == "updated"


def test_doctor_reports_presence_without_exposing_values(tmp_path, monkeypatch):
    module = load_installer()
    state_dir = tmp_path / ".napseer"
    state_dir.mkdir()
    auth_path = state_dir / "auth.json"
    auth_path.write_text(
        json.dumps(
            {
                "account_mode": "anonymous",
                "project_id": "test-project",
                "token": "test-access-value",
                "refresh_token": "test-refresh-value",
            }
        ),
        encoding="utf-8",
    )
    install_dir = tmp_path / "install"
    install_dir.mkdir()
    (install_dir / "napseer_mcp_server.py").write_text("# worker\n", encoding="utf-8")
    (install_dir / "napseer_mcp_supervisor.py").write_text("# supervisor\n", encoding="utf-8")
    monkeypatch.setattr(module, "INSTALL_DIR", install_dir)
    monkeypatch.setattr(module, "runtime_assets_missing", lambda: [])
    monkeypatch.setattr(module, "mcp_runtime_probe", lambda: {"status": "ok", "transport": True, "read": True, "tool_count": 56})
    monkeypatch.setattr(
        module,
        "state_dir_status",
        lambda: {
            "active_auth_path": str(auth_path),
            "state_dir_exists": True,
        },
    )

    result = module.doctor_status()
    serialized = json.dumps(result)

    assert result["status"] == "ok"
    assert result["checks"]["token_present"] is True
    assert result["checks"]["refresh_present"] is True
    assert "test-access-value" not in serialized
    assert "test-refresh-value" not in serialized


def test_doctor_does_not_create_state_directory(tmp_path, monkeypatch):
    module = load_installer()
    state_dir = tmp_path / ".napseer"
    install_dir = tmp_path / "install"
    install_dir.mkdir()
    (install_dir / "napseer_mcp_server.py").write_text("# worker\n", encoding="utf-8")
    (install_dir / "napseer_mcp_supervisor.py").write_text("# supervisor\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(module, "INSTALL_DIR", install_dir)
    monkeypatch.setattr(module, "runtime_assets_missing", lambda: [])
    monkeypatch.setattr(module, "mcp_runtime_probe", lambda: {"status": "failed", "transport": False, "read": False})

    result = module.doctor_status()

    assert result["status"] == "repair_required"
    assert not state_dir.exists()


def test_wrapper_dispatch_never_installs_missing_runtime(monkeypatch):
    module = load_installer()
    monkeypatch.setattr(module, "runtime_assets_missing", lambda: ["napseer_mcp_server.py"])
    monkeypatch.setattr(module, "install_assets", fail_if_called)

    try:
        module.run_wrapper("configure", [])
    except RuntimeError as exc:
        assert "nap mcp install" in str(exc)
    else:
        raise AssertionError("missing runtime should require an explicit repair command")


def fake_bundle(module, revision="a" * 40, broken_name=None):
    contents = {
        name: f"#!/usr/bin/env python3\nVALUE = {index!r}\n"
        for index, name in enumerate(module.SCRIPT_NAMES)
    }
    items = []
    payloads = {}
    for name in module.SCRIPT_NAMES:
        content = contents[name]
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        item = {
            "name": name,
            "version": module.CLI_RELEASE_VERSION,
            "bytes": len(content.encode("utf-8")),
            "sha256": digest,
            "source_repo": "https://github.com/napseer/nap-cli",
            "source_revision": revision,
            "source_revision_status": "resolved",
            "contract_version": module.CLI_DISTRIBUTION_CONTRACT_VERSION,
            "minimum_contract_version": module.CLI_MINIMUM_CONTRACT_VERSION,
            "install_path": module.INSTALL_PATHS[name],
            "mode": "0755",
        }
        items.append(item)
        payloads[name] = ({**item, "content": content}, content)
    if broken_name:
        payload, content = payloads[broken_name]
        payloads[broken_name] = (payload, content + "not valid python !!!")
    manifest = {
        "schema_version": module.CLI_BUNDLE_SCHEMA_VERSION,
        "bundle_id": f"nap-cli:{module.CLI_RELEASE_VERSION}:{revision}",
        "release_version": module.CLI_RELEASE_VERSION,
        "published_at": "2026-07-29T00:00:00Z",
        "contract": {
            "current": module.CLI_DISTRIBUTION_CONTRACT_VERSION,
            "minimum_supported": module.CLI_MINIMUM_CONTRACT_VERSION,
        },
        "source": {
            "repo": "https://github.com/napseer/nap-cli",
            "revision": revision,
            "revision_status": "resolved",
        },
        "items": items,
    }
    return manifest, payloads


def test_bundle_install_stages_validates_and_activates_atomically(tmp_path, monkeypatch):
    module = load_installer()
    install_dir = tmp_path / "install"
    bin_dir = tmp_path / "bin"
    monkeypatch.setattr(module, "INSTALL_DIR", install_dir)
    monkeypatch.setattr(module, "BIN_DIR", bin_dir)
    manifest, payloads = fake_bundle(module)
    monkeypatch.setattr(module, "bundle_manifest", lambda: manifest)
    monkeypatch.setattr(module, "fetch_script", lambda name: payloads[name])

    result = module.install_assets()

    assert result["cli_distribution"]["release_version"] == "0.2.1"
    assert (install_dir / "current").is_symlink()
    assert (install_dir / "current" / "bundle-manifest.json").is_file()
    assert (install_dir / "current" / "napseer_mcp_server.py").is_file()
    assert (install_dir / "current" / "terminal" / "protocol.py").is_file()
    assert (install_dir / "napseer_mcp_supervisor.py").is_symlink()
    assert (bin_dir / "nap").is_symlink()
    assert module.version_status()["source_revision"] == "a" * 40


def test_failed_bundle_validation_keeps_previous_release_active(tmp_path, monkeypatch):
    module = load_installer()
    monkeypatch.setattr(module, "INSTALL_DIR", tmp_path / "install")
    monkeypatch.setattr(module, "BIN_DIR", tmp_path / "bin")
    first_manifest, first_payloads = fake_bundle(module, revision="a" * 40)
    monkeypatch.setattr(module, "bundle_manifest", lambda: first_manifest)
    monkeypatch.setattr(module, "fetch_script", lambda name: first_payloads[name])
    module.install_assets()
    active_before = (module.INSTALL_DIR / "current").resolve()

    broken_manifest, broken_payloads = fake_bundle(
        module,
        revision="b" * 40,
        broken_name="napseer_mcp_server.py",
    )
    monkeypatch.setattr(module, "bundle_manifest", lambda: broken_manifest)
    monkeypatch.setattr(module, "fetch_script", lambda name: broken_payloads[name])

    try:
        module.install_assets()
    except RuntimeError as exc:
        assert "does not match bundle manifest" in str(exc)
    else:
        raise AssertionError("invalid bundle must not activate")

    assert (module.INSTALL_DIR / "current").resolve() == active_before
    assert not list((module.INSTALL_DIR / "releases").glob(".staging-*"))


def test_manifest_rejects_path_traversal():
    module = load_installer()
    manifest, _payloads = fake_bundle(module)
    target = next(item for item in manifest["items"] if item["name"] == "nap_install.py")
    target["install_path"] = "../nap_install.py"

    try:
        module.validated_manifest_items(manifest)
    except RuntimeError as exc:
        assert "unsafe install_path" in str(exc)
    else:
        raise AssertionError("unsafe manifest path must be rejected")


def test_manifest_accepts_a_future_compatible_release():
    module = load_installer()
    manifest, _payloads = fake_bundle(module)
    manifest["release_version"] = "0.2.2"
    manifest["contract"]["current"] = "2026-08-02"
    for item in manifest["items"]:
        item["version"] = "0.2.2"
        item["contract_version"] = "2026-08-02"

    items = module.validated_manifest_items(manifest)

    assert len(items) == len(module.SCRIPT_NAMES)


def test_manifest_rejects_a_minimum_contract_newer_than_installer():
    module = load_installer()
    manifest, _payloads = fake_bundle(module)
    manifest["contract"]["current"] = "2026-08-02"
    manifest["contract"]["minimum_supported"] = "2026-08-02"
    for item in manifest["items"]:
        item["contract_version"] = "2026-08-02"
        item["minimum_contract_version"] = "2026-08-02"

    try:
        module.validated_manifest_items(manifest)
    except RuntimeError as exc:
        assert "newer bootstrap installer" in str(exc)
    else:
        raise AssertionError("incompatible minimum contract must be rejected")


def test_cli_log_compaction_preserves_live_writer_inode(tmp_path):
    module = load_installer()
    log_path = tmp_path / "gateway.log"
    log_path.write_bytes((b"old-line\n" * 200) + b"recent-line\n")
    inode_before = log_path.stat().st_ino

    with log_path.open("ab", buffering=0) as live_writer:
        module.compact_log_file(log_path, 128)
        live_writer.write(b"after-cli-compaction\n")

    assert log_path.stat().st_ino == inode_before
    content = log_path.read_bytes()
    assert content.endswith(b"after-cli-compaction\n")
    assert b"recent-line\n" in content
