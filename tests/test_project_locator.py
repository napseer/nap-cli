#!/usr/bin/env python3
"""Contract tests for the committed, non-secret Napseer project locator."""

import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile


PROJECT_ID = "33333333-3333-3333-3333-333333333333"
OTHER_PROJECT_ID = "44444444-4444-4444-4444-444444444444"


def load_module():
    script_path = pathlib.Path(__file__).resolve().parents[1] / "resources" / "scripts" / "napseer_mcp_server.py"
    sys.path.insert(0, str(script_path.parent))
    spec = importlib.util.spec_from_file_location("napseer_mcp_server_project_locator_test", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def configure_state(mod, root):
    state_dir = pathlib.Path(root) / ".napseer"
    mod.AUTH_DIR = state_dir
    mod.AUTH_PATH = state_dir / "auth.json"
    mod.BASE_URL = "https://api.example.test"
    mod.TOKEN = None
    mod.DEFAULT_PROJECT_ID = None
    mod.AUTH = {}
    return state_dir


def test_locator_is_deterministic_committable_and_secret_free():
    mod = load_module()
    with tempfile.TemporaryDirectory() as directory:
        root = pathlib.Path(directory)
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        state_dir = configure_state(mod, root)
        mod.DEFAULT_PROJECT_ID = PROJECT_ID

        result = mod.write_project_locator({"id": PROJECT_ID, "slug": "portable-project"})
        locator = json.loads((state_dir / "project.json").read_text(encoding="utf-8"))

        assert locator == {
            "schema": "napseer.project.v1",
            "base_url": "https://api.example.test",
            "project_id": PROJECT_ID,
            "project_slug": "portable-project",
        }
        assert result["declared"] is True
        assert result["matches_local_auth"] is True
        forbidden = {
            "token", "access_token", "refresh_token", "account_id", "agent_id",
            "worker_id", "claim_token", "claim_url", "passphrase", "encryption_state",
        }
        assert forbidden.isdisjoint(locator)

        (state_dir / "auth.json").write_text('{"token":"private"}\n', encoding="utf-8")
        (state_dir / "gateway-auth.json").write_text('{"token":"private"}\n', encoding="utf-8")
        assert subprocess.run(
            ["git", "check-ignore", "--quiet", ".napseer/auth.json"], cwd=root
        ).returncode == 0
        assert subprocess.run(
            ["git", "check-ignore", "--quiet", ".napseer/gateway-auth.json"], cwd=root
        ).returncode == 0
        assert subprocess.run(
            ["git", "check-ignore", "--quiet", ".napseer/project.json"], cwd=root
        ).returncode == 1
        assert subprocess.run(
            ["git", "check-ignore", "--quiet", ".napseer/.gitignore"], cwd=root
        ).returncode == 1


def test_locator_rejects_extra_secret_fields_and_mismatched_auth():
    mod = load_module()
    with tempfile.TemporaryDirectory() as directory:
        state_dir = configure_state(mod, directory)
        state_dir.mkdir(parents=True)
        (state_dir / "project.json").write_text(
            json.dumps({
                "schema": "napseer.project.v1",
                "base_url": "https://api.example.test",
                "project_id": PROJECT_ID,
                "project_slug": "portable-project",
                "token": "must-not-be-accepted",
            }),
            encoding="utf-8",
        )
        try:
            mod.load_project_locator()
        except mod.SafeToolError as exc:
            assert exc.code == "project_locator_invalid"
        else:
            raise AssertionError("locator with an extra secret field must be rejected")

        (state_dir / "project.json").unlink()
        mod.write_project_locator({"id": PROJECT_ID, "slug": "portable-project"})
        mod.DEFAULT_PROJECT_ID = OTHER_PROJECT_ID
        mod.refresh_public_auth_state = lambda: None
        try:
            mod.resolve_project_id({})
        except mod.SafeToolError as exc:
            assert exc.code == "project_locator_mismatch"
        else:
            raise AssertionError("local auth/locator mismatch must fail closed")


def test_fresh_clone_requires_attach_and_never_bootstraps_anonymous_project():
    mod = load_module()
    with tempfile.TemporaryDirectory() as directory:
        configure_state(mod, directory)
        mod.write_project_locator({"id": PROJECT_ID, "slug": "portable-project"})
        enrollment_calls = []
        bootstrap_calls = []
        mod.ensure_enrolled = lambda args: enrollment_calls.append(dict(args))
        mod.bootstrap_project = lambda args: bootstrap_calls.append(dict(args))

        result = mod.cli_project_init([])
        assert result["status"] == "project_access_required"
        assert result["project_id"] == PROJECT_ID
        assert result["next"]["attach_project"] == "nap project attach"
        assert enrollment_calls == []
        assert bootstrap_calls == []

        mod.refresh_public_auth_state = lambda: None
        try:
            mod.resolve_project_id({})
        except mod.SafeToolError as exc:
            assert exc.code == "project_access_required"
            assert "will not create a duplicate anonymous project" in exc.safe_message
        else:
            raise AssertionError("first MCP project resolution must require attach")


def test_attach_forwards_and_verifies_repository_project_hint():
    mod = load_module()
    with tempfile.TemporaryDirectory() as directory:
        configure_state(mod, directory)
        mod.write_project_locator({"id": PROJECT_ID, "slug": "portable-project"})
        oauth_calls = []
        saves = []

        def oauth(args):
            oauth_calls.append(dict(args))
            return {
                "api_base_url": "https://api.example.test",
                "access_token": "local-test-token",
                "refresh_token": "local-test-refresh",
                "expires_in": 3600,
                "scope": args["scope"],
                "project_id": PROJECT_ID,
            }

        mod.oauth_loopback_authorize = oauth
        mod.replace_public_auth_state = lambda updates, clear_keys=(): saves.append(dict(updates))
        mod.save_auth_file_credentials = lambda updates: saves.append(dict(updates))
        mod.request_json = lambda method, path: {
            "id": PROJECT_ID,
            "slug": "portable-project",
            "name": "Portable Project",
            "encryption_state": "standard",
        }

        result = mod.operator_project_attach({"open_browser": False})
        assert oauth_calls[0]["project_id_hint"] == PROJECT_ID
        assert result["project_id"] == PROJECT_ID
        assert result["project_locator"]["project_id"] == PROJECT_ID
        assert saves[0]["project_id"] == PROJECT_ID


if __name__ == "__main__":
    test_locator_is_deterministic_committable_and_secret_free()
    test_locator_rejects_extra_secret_fields_and_mismatched_auth()
    test_fresh_clone_requires_attach_and_never_bootstraps_anonymous_project()
    test_attach_forwards_and_verifies_repository_project_hint()
    print("ok: committed project locator contract passed")
