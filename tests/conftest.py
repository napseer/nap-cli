"""All tests use disposable user credential state, including legacy test scripts."""
import pytest


@pytest.fixture(autouse=True)
def isolated_user_credentials(monkeypatch, tmp_path):
    monkeypatch.setenv("NAPSEER_USER_DATA_DIR", str(tmp_path / "user-data"))
    for name in ("NAPSEER_API_KEY", "NAPSEER_TOKEN", "NAPSEER_REFRESH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
