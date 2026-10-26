import asyncio
import json
import os

import pytest

import api.config
from core import llm_client
from core.secrets import mask_secrets
from api.routes.routes_stats import toggle_model_logging


class _NullConnection:
    def execute(self, *args):
        return self

    def commit(self):
        pass

    def close(self):
        pass


def test_model_logging_defaults_off():
    assert llm_client._model_logging_enabled is False


def test_model_logging_respects_saved_config(monkeypatch):
    monkeypatch.setattr(api.config, "load_config", lambda: {"model_logging_enabled": True})
    monkeypatch.setattr(llm_client, "_model_logging_config_loaded", False, raising=False)
    monkeypatch.setattr(llm_client, "_model_logging_enabled", False)

    assert llm_client.is_model_logging_enabled() is True


def test_toggle_updates_runtime(monkeypatch):
    saved = {}
    monkeypatch.setattr(api.config, "load_config", lambda: {})
    monkeypatch.setattr(api.config, "save_config", lambda cfg: saved.update(cfg))
    llm_client.set_model_logging(False)

    result = asyncio.run(toggle_model_logging({"enabled": True}))

    assert result == {"enabled": True}
    assert saved["model_logging_enabled"] is True
    assert llm_client.is_model_logging_enabled() is True
    llm_client.set_model_logging(False)


def test_model_log_files_mask_nested_credentials(monkeypatch, tmp_path):
    from core import paths

    monkeypatch.setattr(paths, "get_data_path", lambda name: str(tmp_path / name))
    monkeypatch.setattr(llm_client, "db_connect", lambda: _NullConnection())
    monkeypatch.setattr(llm_client, "_init_model_logs_table", lambda: None)
    llm_client.set_model_logging(True)
    request = json.dumps({
        "messages": [{"role": "user", "content": "hello"}],
        "metadata": [{
            "Authorization": "Bearer request-secret",
            "api_key": "request-api-key",
            "cookie": "session=request-cookie",
            "password": "request-password",
        }],
    })

    llm_client._log_model_call(
        "openai", "gpt-test", 1, 1, 2, request, '{"ok": true}',
        "miss", 1,
    )
    llm_client.set_model_logging(False)

    req_files = list((tmp_path / "logs" / "model_calls").glob("*_req.json"))
    assert len(req_files) == 1
    contents = req_files[0].read_text(encoding="utf-8")
    for plaintext in (
        "request-secret", "request-api-key", "request-cookie", "request-password",
    ):
        assert plaintext not in contents
    assert "***" in contents
    if os.name != "nt":
        assert req_files[0].stat().st_mode & 0o777 == 0o600


def test_recursive_mask_handles_nested_dicts_and_lists():
    from core.secrets import mask_sensitive_data

    value = {
        "outer": [{
            "Authorization": "Bearer nested-secret",
            "API_KEY": "nested-api-key",
            "Cookie": "nested-cookie",
            "password": "nested-password",
        }],
        "ordinary": "visible",
    }

    masked = mask_sensitive_data(value)

    assert masked["outer"][0] == {
        "Authorization": "***",
        "API_KEY": "***",
        "Cookie": "***",
        "password": "***",
    }
    assert masked["ordinary"] == "visible"
    assert value["outer"][0]["Authorization"] == "Bearer nested-secret"


def test_mask_secrets_covers_configured_credentials(monkeypatch):
    monkeypatch.setattr(api.config, "load_config", lambda: {
        "api_keys": {"openai": "configured-api-key"},
        "email_password": "configured-email-password",
        "mcp_servers": {"remote": {"headers": {"Authorization": "Bearer configured-bearer"}}},
    })

    masked = mask_secrets(
        "configured-api-key configured-email-password Bearer configured-bearer"
    )

    assert "configured-api-key" not in masked
    assert "configured-email-password" not in masked
    assert "configured-bearer" not in masked


def test_update_setting_echo_is_masked(monkeypatch, tmp_path):
    from tools import system_config

    monkeypatch.setattr(system_config, "CONFIG_PATH", str(tmp_path / "config.json"))
    value = {
        "remote": {
            "headers": {"Authorization": "Bearer update-secret"},
            "api_key": "update-api-key",
        }
    }

    result = system_config.ConfigureSystemTool()._update_setting(
        "mcp_servers", json.dumps(value)
    )

    assert "update-secret" not in result
    assert "update-api-key" not in result
    assert "***" in result
    stored = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert stored["mcp_servers"] == value


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_secrets_file_permissions(monkeypatch, tmp_path):
    from core.secrets import upsert_secret

    monkeypatch.setenv("OPEN_AGC_DATA_DIR", str(tmp_path))
    upsert_secret(name="db", password="secret")

    assert (tmp_path / "secrets.json").stat().st_mode & 0o777 == 0o600
