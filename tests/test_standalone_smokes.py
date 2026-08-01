"""Make supported standalone smoke programs part of the pytest gate."""

import pathlib
import subprocess
import sys

import pytest


SMOKES = [
    "test_anonymous_auth_recovery.py",
    "test_chat_secret_cli_flow.py",
    "test_cli_dispatch_consolidation.py",
    "test_gateway_cloud_routes_removed.py",
    "test_gateway_configure_consistency.py",
    "test_gateway_local_auth_refresh.py",
    "test_gateway_local_vault_key.py",
    "test_gateway_vault_setup_payload.py",
    "test_live_auth_state_reload.py",
    "test_mcp_memory_ergonomics_helpers.py",
    "test_mcp_memory_node_encryption.py",
    "test_nap_install_gateway_routes.py",
    "test_project_attach_oauth.py",
    "test_project_encryption_enable_payload.py",
    "test_project_init_wizard.py",
    "test_two_passphrase_boundary.py",
]


@pytest.mark.parametrize("filename", SMOKES)
def test_standalone_smoke(filename):
    test_dir = pathlib.Path(__file__).resolve().parent
    result = subprocess.run(
        [sys.executable, str(test_dir / filename)],
        cwd=test_dir.parent,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 0, (
        f"{filename} failed with exit {result.returncode}\n"
        f"stdout:\n{result.stdout[-4000:]}\n"
        f"stderr:\n{result.stderr[-4000:]}"
    )
