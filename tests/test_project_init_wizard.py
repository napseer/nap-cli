#!/usr/bin/env python3
"""Smoke-test the `nap project init` first-time setup wizard."""

import importlib.util
import pathlib
import sys


PROJECT_ID = "22222222-2222-2222-2222-222222222222"


def load_module():
    script_path = pathlib.Path(__file__).resolve().parents[1] / "resources" / "scripts" / "napseer_mcp_server.py"
    sys.path.insert(0, str(script_path.parent))
    spec = importlib.util.spec_from_file_location("napseer_mcp_server_project_init_test", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_init_fresh_directory():
    mod = load_module()
    enrollment_calls = []
    bootstrap_calls = []
    saved_payloads = []

    mod.TOKEN = None
    mod.DEFAULT_PROJECT_ID = None
    mod.AUTH = {}
    mod.AUTH_PATH = pathlib.Path("/tmp/napseer-test/auth.json")
    mod.BASE_URL = "https://api.example.test"

    mod.load_public_auth_file = lambda: {}

    def fake_ensure_enrolled(args):
        enrollment_calls.append(args)
        mod.TOKEN = "ssh-token"
        return {"status": "enrolled", "token_expires_at": "2099-01-01T00:00:00Z"}

    def fake_bootstrap_project(args):
        bootstrap_calls.append(args)
        mod.DEFAULT_PROJECT_ID = PROJECT_ID
        return {
            "status": "created",
            "project": {"id": PROJECT_ID, "slug": args["slug"], "name": args["name"]},
            "project_id": PROJECT_ID,
            "encryption_state": "plaintext",
            "project_unlocked": True,
            "message": "created",
        }

    mod.ensure_enrolled = fake_ensure_enrolled
    mod.bootstrap_project = fake_bootstrap_project
    mod.save_auth = lambda updates: saved_payloads.append(dict(updates))

    result = mod.cli_project_init([])

    assert result["status"] == "initialized", result
    assert result["project_id"] == PROJECT_ID
    assert result["project"]["slug"], result
    assert any(step.get("step") == "enroll" for step in result["steps"]), result["steps"]
    assert any(step.get("step") == "create_project" for step in result["steps"]), result["steps"]
    assert enrollment_calls, "ensure_enrolled should have been called"
    assert bootstrap_calls, "bootstrap_project should have been called"
    print("ok: project init fresh-directory wizard passed")


def test_init_already_initialized():
    mod = load_module()
    mod.TOKEN = "existing-token"
    mod.DEFAULT_PROJECT_ID = PROJECT_ID
    mod.AUTH = {"project_slug": "my-existing"}
    mod.AUTH_PATH = pathlib.Path("/tmp/napseer-test/auth.json")

    mod.load_public_auth_file = lambda: {"project_slug": "my-existing"}

    enrollment_called = []
    bootstrap_called = []

    def fake_ensure_enrolled(args):
        enrollment_called.append(args)
        return {"status": "enrolled"}

    def fake_bootstrap_project(args):
        bootstrap_called.append(args)
        return {}

    mod.ensure_enrolled = fake_ensure_enrolled
    mod.bootstrap_project = fake_bootstrap_project

    result = mod.cli_project_init([])

    assert result["status"] == "already_initialized", result
    assert result["project_slug"] == "my-existing", result
    assert not enrollment_called, "ensure_enrolled should NOT have been called"
    assert not bootstrap_called, "bootstrap_project should NOT have been called"
    print("ok: project init no-op on already-initialized folder passed")


def test_init_token_only_creates_project():
    mod = load_module()
    mod.TOKEN = "already-have-token"
    mod.DEFAULT_PROJECT_ID = None
    mod.AUTH = {}
    mod.AUTH_PATH = pathlib.Path("/tmp/napseer-test/auth.json")

    mod.load_public_auth_file = lambda: {}

    enrollment_called = []

    def fake_ensure_enrolled(args):
        enrollment_called.append(args)
        return {"status": "enrolled"}

    def fake_bootstrap_project(args):
        mod.DEFAULT_PROJECT_ID = PROJECT_ID
        return {
            "status": "created",
            "project": {"id": PROJECT_ID, "slug": args["slug"], "name": args["name"]},
            "project_id": PROJECT_ID,
            "encryption_state": "plaintext",
            "project_unlocked": True,
            "message": "created",
        }

    mod.ensure_enrolled = fake_ensure_enrolled
    mod.bootstrap_project = fake_bootstrap_project

    result = mod.cli_project_init(["--slug", "my-slug", "--name", "My Name"])

    assert result["status"] == "initialized", result
    assert result["project_id"] == PROJECT_ID, result
    assert not enrollment_called, "ensure_enrolled should NOT be called when TOKEN is already set"
    enroll_step = next(s for s in result["steps"] if s["step"] == "enroll")
    assert enroll_step["status"] == "skipped", enroll_step
    create_step = next(s for s in result["steps"] if s["step"] == "create_project")
    assert create_step["status"] == "created", create_step
    assert fake_bootstrap_project.__call__ is not None
    print("ok: project init creates project when token exists passed")


def test_cli_main_dispatches_init():
    """Verify the cli_main dispatch routes `project init` to cli_project_init."""
    import io
    import contextlib
    mod = load_module()
    mod.TOKEN = "t"
    mod.DEFAULT_PROJECT_ID = PROJECT_ID
    mod.AUTH = {"project_slug": "x"}
    mod.AUTH_PATH = pathlib.Path("/tmp/napseer-test/auth.json")
    mod.load_public_auth_file = lambda: {"project_slug": "x"}

    captured = []
    mod.cli_project_init = lambda args: (captured.append(args) or {"status": "already_initialized"})

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = mod.cli_main(["napseer_mcp_server.py", "project", "init", "--no-browser"])
    assert rc is None
    assert captured == [["--no-browser"]]
    assert '"already_initialized"' in buf.getvalue(), buf.getvalue()
    print("ok: cli_main routes `project init` to cli_project_init passed")


def test_cli_main_rejects_old_aliases():
    """Verify `auth login` / `project bootstrap-existing` are no longer accepted."""
    mod = load_module()

    cases = [
        (["napseer_mcp_server.py", "auth", "login"], "unknown auth subcommand: login"),
        (["napseer_mcp_server.py", "auth", "operator-login"], "unknown auth subcommand: operator-login"),
        (["napseer_mcp_server.py", "project", "login"], "unknown project subcommand: login"),
        (["napseer_mcp_server.py", "project", "bootstrap-existing"], "unknown project subcommand: bootstrap-existing"),
    ]
    for args, expected_substring in cases:
        try:
            mod.cli_main(args)
        except RuntimeError as exc:
            assert expected_substring in str(exc), (args, str(exc))
        else:
            raise AssertionError(f"expected RuntimeError for {args}")
    print("ok: cli_main rejects old auth/project aliases with clear errors passed")


if __name__ == "__main__":
    test_init_fresh_directory()
    test_init_already_initialized()
    test_init_token_only_creates_project()
    test_cli_main_dispatches_init()
    test_cli_main_rejects_old_aliases()
    print("ok: project init wizard smoke tests passed")
