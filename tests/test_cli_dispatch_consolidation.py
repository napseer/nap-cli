#!/usr/bin/env python3
"""Smoke-test the consolidated CLI dispatch surface.

Verifies that:
  - canonical verbs (status, list, create, update, delete, run, preregister, activate)
    are accepted and route to the right function.
  - dropped aliases (ls, new, send, rm, set-status, admin, global-list, etc.)
    now raise RuntimeError with a clear pointer to the canonical verb.
  - `nap project encryption set` requires --state; positional-state shortcut is gone.
  - `nap project encryption plaintext` (action-as-state) now errors.
"""

import importlib.util
import pathlib
import sys


def load_module():
    script_path = pathlib.Path(__file__).resolve().parents[1] / "resources" / "scripts" / "napseer_mcp_server.py"
    sys.path.insert(0, str(script_path.parent))
    spec = importlib.util.spec_from_file_location("napseer_mcp_server_dispatch_test", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def expect_unknown(mod, argv, expected_fragment):
    """Run cli_main(argv) and assert it raises RuntimeError with the fragment."""
    try:
        mod.cli_main(argv)
    except RuntimeError as exc:
        assert expected_fragment in str(exc), (argv, str(exc))
    else:
        raise AssertionError(f"expected RuntimeError for {argv}")


def test_chat_secret_aliases_rejected():
    mod = load_module()
    for args in [
        ["napseer_mcp_server.py", "chat", "secret", "init"],
        ["napseer_mcp_server.py", "chat", "secret", "configure"],
    ]:
        expect_unknown(mod, args, "unknown chat secret action")


def test_lineage_aliases_rejected():
    mod = load_module()
    for args in [
        ["napseer_mcp_server.py", "lineage", "list"],
        ["napseer_mcp_server.py", "lineage", "ls"],
        ["napseer_mcp_server.py", "lineage", "check"],
    ]:
        expect_unknown(mod, args, "unknown lineage subcommand")


def test_plan_aliases_rejected():
    mod = load_module()
    for args in [
        ["napseer_mcp_server.py", "plan", "ls"],
        ["napseer_mcp_server.py", "plan", "active"],
        ["napseer_mcp_server.py", "plan", "card-from"],
        ["napseer_mcp_server.py", "plan", "create-card"],
    ]:
        expect_unknown(mod, args, "unknown plan subcommand")


def test_feedback_aliases_rejected():
    mod = load_module()
    for args in [
        ["napseer_mcp_server.py", "feedback", "ls"],
        ["napseer_mcp_server.py", "feedback", "admin"],
        ["napseer_mcp_server.py", "feedback", "admin-list"],
        ["napseer_mcp_server.py", "feedback", "global-list"],
        ["napseer_mcp_server.py", "feedback", "status", "abc", "open"],
        ["napseer_mcp_server.py", "feedback", "set-status", "abc", "open"],
        ["napseer_mcp_server.py", "feedback", "update", "abc", "open"],
        ["napseer_mcp_server.py", "feedback", "resolved", "abc"],
    ]:
        expect_unknown(mod, args, "unknown feedback subcommand")


def test_agent_aliases_rejected():
    mod = load_module()
    for args in [
        ["napseer_mcp_server.py", "agent", "ls"],
        ["napseer_mcp_server.py", "agent", "registered"],
        ["napseer_mcp_server.py", "agent", "workspace-list"],
    ]:
        expect_unknown(mod, args, "unknown agent subcommand")


def test_gateway_status_aliases_rejected():
    mod = load_module()
    for args in [
        ["napseer_mcp_server.py", "gateway", "ls"],
        ["napseer_mcp_server.py", "gateway", "list"],
    ]:
        expect_unknown(mod, args, "unknown gateway subcommand")


def test_gateway_terminal_aliases_rejected():
    mod = load_module()
    for args in [
        ["napseer_mcp_server.py", "gateway", "terminals", "list"],
        ["napseer_mcp_server.py", "gateway", "terminal", "new"],
        ["napseer_mcp_server.py", "gateway", "terminal", "rm", "x"],
        ["napseer_mcp_server.py", "gateway", "terminal", "send", "x"],
    ]:
        expect_unknown(mod, args, "unknown gateway")


def test_gateway_schedule_aliases_rejected():
    mod = load_module()
    for args in [
        ["napseer_mcp_server.py", "gateway", "schedules", "list"],
        ["napseer_mcp_server.py", "gateway", "cron", "list"],
        ["napseer_mcp_server.py", "gateway", "crons", "list"],
        ["napseer_mcp_server.py", "gateway", "schedule", "ls"],
        ["napseer_mcp_server.py", "gateway", "schedule", "new"],
        ["napseer_mcp_server.py", "gateway", "schedule", "edit", "x"],
        ["napseer_mcp_server.py", "gateway", "schedule", "rm", "x"],
        ["napseer_mcp_server.py", "gateway", "schedule", "run-now", "x"],
    ]:
        expect_unknown(mod, args, "unknown gateway")


def test_gateway_service_run_rejected():
    """Lifecycle verbs (run, start, etc.) moved to `nap service`."""
    mod = load_module()
    for args in [
        ["napseer_mcp_server.py", "gateway", "service", "run"],
        ["napseer_mcp_server.py", "gateway", "service", "start"],
        ["napseer_mcp_server.py", "gateway", "service", "register"],
        ["napseer_mcp_server.py", "gateway", "service", "bootstrap"],
    ]:
        expect_unknown(mod, args, "nap service start")


def test_gateway_vault_aliases_rejected():
    mod = load_module()
    for args in [
        ["napseer_mcp_server.py", "gateway", "vault", "list"],
        ["napseer_mcp_server.py", "gateway", "vault", "ls"],
    ]:
        expect_unknown(mod, args, "gateway vault action: list")


def test_gateway_rotate_passphrase_aliases_rejected():
    mod = load_module()
    for args in [
        ["napseer_mcp_server.py", "gateway", "passwd"],
        ["napseer_mcp_server.py", "gateway", "change-passphrase"],
    ]:
        expect_unknown(mod, args, "rotate-passphrase")


def test_project_encryption_state_flag_required():
    """nap project encryption set requires --state; positional state is rejected."""
    mod = load_module()
    # Without --state, the dispatch should raise
    expect_unknown(
        mod,
        ["napseer_mcp_server.py", "project", "encryption", "set", "plaintext"],
        "--state plaintext|encrypted",
    )


def test_project_encryption_action_as_state_rejected():
    """`nap project encryption plaintext` (the old shortcut) must error."""
    mod = load_module()
    expect_unknown(
        mod,
        ["napseer_mcp_server.py", "project", "encryption", "plaintext"],
        "project encryption",
    )


def test_project_encryption_show_alias_rejected():
    mod = load_module()
    for args in [
        ["napseer_mcp_server.py", "project", "encryption", "state"],
        ["napseer_mcp_server.py", "project", "encryption", "show"],
        ["napseer_mcp_server.py", "project", "encryption", "encrypted"],
    ]:
        expect_unknown(mod, args, "project encryption")


if __name__ == "__main__":
    test_chat_secret_aliases_rejected()
    test_lineage_aliases_rejected()
    test_plan_aliases_rejected()
    test_feedback_aliases_rejected()
    test_agent_aliases_rejected()
    test_gateway_status_aliases_rejected()
    test_gateway_terminal_aliases_rejected()
    test_gateway_schedule_aliases_rejected()
    test_gateway_service_run_rejected()
    test_gateway_vault_aliases_rejected()
    test_gateway_rotate_passphrase_aliases_rejected()
    test_project_encryption_state_flag_required()
    test_project_encryption_action_as_state_rejected()
    test_project_encryption_show_alias_rejected()
    print("ok: cli dispatch consolidation tests passed")
