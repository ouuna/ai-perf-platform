"""配置加载：环境变量优先，其次 .env 文件，最后默认值。

使用 pydantic-settings 风格的轻量实现（不额外引入依赖），
保证所有配置可被环境变量覆盖，无硬编码密钥。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env_bool(key: str, default: bool) -> bool:
    raw = os.getenv(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(key: str, default: int) -> int:
    raw = os.getenv(key)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


@dataclass
class Settings:
    # 存储
    db_path: str = field(default_factory=lambda: os.getenv("AP_DB_PATH", "data/platform.db"))
    data_dir: str = field(default_factory=lambda: os.getenv("AP_DATA_DIR", "data"))

    # LLM
    llm_provider: str = field(default_factory=lambda: os.getenv("AP_LLM_PROVIDER", "mock"))
    anthropic_api_key: str = field(default_factory=lambda: os.getenv("ANTHROPIC_API_KEY", ""))
    openai_api_key: str = field(default_factory=lambda: os.getenv("OPENAI_API_KEY", ""))
    openai_base_url: str = field(default_factory=lambda: os.getenv("OPENAI_BASE_URL", ""))
    llm_model: str = field(default_factory=lambda: os.getenv("AP_LLM_MODEL", ""))

    # Safety 硬上限
    max_users: int = field(default_factory=lambda: _env_int("AP_MAX_USERS", 2000))
    max_duration_seconds: int = field(default_factory=lambda: _env_int("AP_MAX_DURATION", 3600))
    max_rps: int = field(default_factory=lambda: _env_int("AP_MAX_RPS", 10000))
    # 默认仅允许 localhost；显式授权域名通过逗号分隔的环境变量注入
    allowed_hosts: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            h.strip()
            for h in os.getenv("AP_ALLOWED_HOSTS", "localhost,127.0.0.1,::1").split(",")
            if h.strip()
        )
    )

    # Runner
    runner_timeout_seconds: int = field(default_factory=lambda: _env_int("AP_RUNNER_TIMEOUT", 1200))
    sla_error_rate_break: float = field(
        default_factory=lambda: float(os.getenv("AP_SLA_ERROR_BREAK", "0.30"))
    )
    default_locust_bin: str = field(default_factory=lambda: os.getenv("AP_LOCUST_BIN", "locust"))

    @property
    def has_llm_key(self) -> bool:
        return bool(self.anthropic_api_key or self.openai_api_key)


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
        Path(_settings.data_dir).mkdir(parents=True, exist_ok=True)
    return _settings
