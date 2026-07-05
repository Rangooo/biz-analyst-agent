"""Contract tests: app_config security boundaries."""
from __future__ import annotations

import os

import pytest

from app_config import (
    assert_custom_provider_allowed,
    assert_runtime_config_allowed,
    load_runtime_config,
)


class TestServerModeSecurity:
    def test_disables_runtime_config(self, monkeypatch):
        monkeypatch.setenv("APP_MODE", "server")
        monkeypatch.delenv("ALLOW_RUNTIME_CONFIG", raising=False)
        cfg = load_runtime_config()
        with pytest.raises(PermissionError):
            assert_runtime_config_allowed(cfg)

    def test_blocks_non_allowlisted_host(self, monkeypatch):
        monkeypatch.setenv("APP_MODE", "server")
        monkeypatch.setenv("ALLOW_CUSTOM_PROVIDER", "true")
        monkeypatch.setenv("CUSTOM_PROVIDER_HOST_ALLOWLIST", "llm-proxy.example.com")
        cfg = load_runtime_config()
        with pytest.raises(ValueError):
            assert_custom_provider_allowed(cfg, "https://untrusted.example.com/v1")

    def test_cors_not_wildcard(self, monkeypatch):
        monkeypatch.setenv("APP_MODE", "server")
        cfg = load_runtime_config()
        assert cfg.cors_allow_headers != ["*"]


class TestLocalMode:
    def test_allows_localhost_provider(self, monkeypatch):
        monkeypatch.setenv("APP_MODE", "local")
        monkeypatch.delenv("ALLOW_CUSTOM_PROVIDER", raising=False)
        cfg = load_runtime_config()
        # Should not raise
        assert_custom_provider_allowed(cfg, "http://localhost:11434/v1")

    def test_cors_permissive(self, monkeypatch):
        monkeypatch.setenv("APP_MODE", "local")
        cfg = load_runtime_config()
        assert cfg.cors_allow_headers == ["*"]


class TestRuntimeConfigKeys:
    def test_apply_runtime_keys_ignores_unknown_data_source_key(self, monkeypatch):
        from main import _apply_runtime_keys

        monkeypatch.delenv("EASTMONEY_API_KEY", raising=False)
        monkeypatch.delenv("EXA_API_KEY", raising=False)

        applied, ignored = _apply_runtime_keys({
            "EXA_API_KEY": "exa-test",
            "EASTMONEY_API_KEY": "not-a-real-setting",
        })

        assert applied == ["EXA_API_KEY"]
        assert ignored == ["EASTMONEY_API_KEY"]
        assert os.getenv("EXA_API_KEY") == "exa-test"
        assert os.getenv("EASTMONEY_API_KEY") is None
