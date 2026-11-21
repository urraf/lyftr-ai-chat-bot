"""Application settings, read once from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache

DEFAULT_SYSTEM_PROMPT = (
    "You are a helpful, concise assistant. Answer clearly and accurately. "
    "Use earlier messages in this conversation as context. Reply in plain text without Markdown formatting."
)


def _split_csv(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def _as_bool(raw: str | None, default: bool = False) -> bool:
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class KeyConfig:
    """One entry of the key pool. `secret` is never logged or returned."""

    key_id: str
    secret: str
    model: str

    def __repr__(self) -> str:  # keep secrets out of logs and tracebacks
        return f"KeyConfig(key_id={self.key_id!r}, model={self.model!r})"


@dataclass(frozen=True)
class Settings:
    keys: list[KeyConfig] = field(default_factory=list)
    database_path: str = "./app.db"
    allowed_origins: list[str] = field(default_factory=list)
    enable_failure_simulation: bool = False
    default_system_prompt: str = DEFAULT_SYSTEM_PROMPT
    cache_type: str = "disk"
    cache_dir: str = "./.llm_cache"
    cache_ttl_seconds: int = 86400
    llm_timeout_seconds: float = 30.0
    max_output_tokens: int = 1024
    temperature: float = 0.2
    max_history_messages: int = 40
    reasoning_effort: str = "low"

    @property
    def cache_enabled(self) -> bool:
        return self.cache_type in {"disk", "local"}


def parse_database_url(url: str) -> str:
    """Turn `sqlite:////data/app.db` / `sqlite:///./app.db` into a filesystem path."""
    url = url.strip()
    if not url.startswith("sqlite:"):
        raise ValueError("DATABASE_URL must be a sqlite URL, e.g. sqlite:////data/app.db")
    path = url[len("sqlite:"):]
    if path.startswith("///"):
        path = path[3:]  # sqlite:////data/app.db -> /data/app.db ; sqlite:///./x -> ./x
    elif path.startswith("//"):
        path = path[2:]
    return path or "./app.db"


def build_key_pool(raw_keys: str | None, default_model: str, raw_models: str | None) -> list[KeyConfig]:
    secrets = _split_csv(raw_keys)
    models = _split_csv(raw_models)
    pool = []
    for index, secret in enumerate(secrets):
        model = models[index] if index < len(models) else default_model
        pool.append(KeyConfig(key_id=f"key_{index + 1}", secret=secret, model=model))
    return pool


def load_settings() -> Settings:
    env = os.environ
    default_model = env.get("LLM_MODEL", "groq/openai/gpt-oss-20b").strip()
    return Settings(
        keys=build_key_pool(env.get("LLM_API_KEYS"), default_model, env.get("LLM_MODELS")),
        database_path=parse_database_url(env.get("DATABASE_URL", "sqlite:///./app.db")),
        allowed_origins=_split_csv(env.get("ALLOWED_ORIGINS", "http://localhost:3000,http://localhost:5173")),
        enable_failure_simulation=_as_bool(env.get("ENABLE_FAILURE_SIMULATION")),
        default_system_prompt=env.get("DEFAULT_SYSTEM_PROMPT", DEFAULT_SYSTEM_PROMPT),
        cache_type=env.get("CACHE_TYPE", "disk").strip().lower(),
        cache_dir=env.get("CACHE_DIR", "./.llm_cache"),
        cache_ttl_seconds=int(env.get("CACHE_TTL_SECONDS", "86400")),
        llm_timeout_seconds=float(env.get("LLM_TIMEOUT_SECONDS", "30")),
        max_output_tokens=int(env.get("LLM_MAX_OUTPUT_TOKENS", "1024")),
        temperature=float(env.get("LLM_TEMPERATURE", "0.2")),
        max_history_messages=int(env.get("MAX_HISTORY_MESSAGES", "40")),
        reasoning_effort=env.get("LLM_REASONING_EFFORT", "low").strip().lower(),
    )


@lru_cache
def get_settings() -> Settings:
    return load_settings()
