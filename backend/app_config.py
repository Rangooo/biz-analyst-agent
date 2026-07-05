"""Runtime configuration and deployment safety guards."""
from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlparse


LOCAL_MODES = {"", "local", "dev", "demo"}
SERVER_MODES = {"server", "prod", "production"}


@dataclass(frozen=True)
class RuntimeConfig:
    app_mode: str
    cors_origins: list[str]
    allow_runtime_config: bool
    allow_custom_provider: bool
    allow_env_persist: bool
    custom_provider_hosts: list[str]

    @property
    def is_server_mode(self) -> bool:
        return self.app_mode in SERVER_MODES

    @property
    def cors_allow_headers(self) -> list[str]:
        """Server 模式收紧为显式白名单，避免任意请求头通配；local 保持宽松便于调试。"""
        if self.is_server_mode:
            return ["Content-Type", "Authorization", "Accept"]
        return ["*"]


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def load_runtime_config() -> RuntimeConfig:
    mode = os.getenv("APP_MODE", "local").strip().lower()
    if mode not in LOCAL_MODES | SERVER_MODES:
        mode = "local"

    cors_origins = _split_csv(os.getenv("CORS_ORIGINS", ""))
    if not cors_origins and mode in LOCAL_MODES:
        cors_origins = [
            "http://localhost:5173",
            "http://127.0.0.1:5173",
            "http://localhost:4173",
            "http://127.0.0.1:4173",
        ]

    server_mode = mode in SERVER_MODES
    return RuntimeConfig(
        app_mode=mode or "local",
        cors_origins=cors_origins,
        allow_runtime_config=_env_flag("ALLOW_RUNTIME_CONFIG", default=not server_mode),
        allow_custom_provider=_env_flag("ALLOW_CUSTOM_PROVIDER", default=not server_mode),
        allow_env_persist=_env_flag("ALLOW_ENV_PERSIST", default=not server_mode),
        custom_provider_hosts=_split_csv(os.getenv("CUSTOM_PROVIDER_HOST_ALLOWLIST", "")),
    )


def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def assert_runtime_config_allowed(config: RuntimeConfig) -> None:
    if not config.allow_runtime_config:
        raise PermissionError(
            "Runtime key configuration is disabled in APP_MODE=server. "
            "Use environment variables or set ALLOW_RUNTIME_CONFIG=true explicitly."
        )


def assert_custom_provider_allowed(config: RuntimeConfig, base_url: str) -> None:
    if not config.allow_custom_provider:
        raise PermissionError(
            "Custom provider writes are disabled in APP_MODE=server. "
            "Set ALLOW_CUSTOM_PROVIDER=true and CUSTOM_PROVIDER_HOST_ALLOWLIST to opt in."
        )
    _validate_custom_provider_url(config, base_url)


def assert_env_persist_allowed(config: RuntimeConfig) -> None:
    if not config.allow_env_persist:
        raise PermissionError(
            "Persisting secrets to .env is disabled. Set ALLOW_ENV_PERSIST=true to opt in."
        )


def _validate_custom_provider_url(config: RuntimeConfig, base_url: str) -> None:
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Custom provider base_url must be an http(s) URL.")

    host = parsed.hostname.lower()
    local_hosts = {"localhost", "127.0.0.1", "::1"}
    if host in local_hosts and not config.is_server_mode:
        return

    allowed = {h.lower() for h in config.custom_provider_hosts}
    if host not in allowed:
        raise ValueError(
            "Custom provider host is not allowlisted. Add it to CUSTOM_PROVIDER_HOST_ALLOWLIST."
        )
