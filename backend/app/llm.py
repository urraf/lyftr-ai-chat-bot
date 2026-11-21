"""LiteLLM gateway: key pool with ordered failover + LiteLLM response cache.

Flow for one request:
  1. Try key_1. LiteLLM first looks the request up in its cache (key = model + full
     message list + generation params; the api_key is NOT part of the key). A hit
     returns immediately without touching the provider -> LLM_CACHE.
  2. On a miss LiteLLM calls the provider with that key. If the provider answers
     429 / quota / 5xx / timeout / connection error, LiteLLM raises a typed
     exception and we move on to key_2, key_3, ... (fallback_attempts += 1).
  3. If every key failed we raise AllProvidersUnavailable -> HTTP 503.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Any

import litellm
from litellm.caching.caching import Cache

from .config import KeyConfig, Settings

logger = logging.getLogger("app.llm")

SIMULATABLE_STATUSES = {429, 500, 502, 503}

LIVE_INFERENCE = "LIVE_INFERENCE"
LLM_CACHE = "LLM_CACHE"


class AllProvidersUnavailable(Exception):
    """Every key in the pool failed for this request."""


@dataclass
class CompletionResult:
    reply: str
    resolution_source: str
    model: str
    input_tokens: int
    output_tokens: int
    key_id: str | None
    fallback_attempts: int


def configure_litellm(settings: Settings) -> None:
    litellm.telemetry = False
    litellm.suppress_debug_info = True
    litellm.drop_params = True  # ignore params a provider does not support instead of erroring
    if not settings.cache_enabled:
        litellm.cache = None
        return
    if settings.cache_type == "disk":
        try:
            os.makedirs(settings.cache_dir, exist_ok=True)
            litellm.cache = Cache(type="disk", disk_cache_dir=settings.cache_dir, ttl=settings.cache_ttl_seconds)
            logger.info("LiteLLM disk cache enabled at %s", settings.cache_dir)
            return
        except Exception:  # pragma: no cover - diskcache missing / unwritable dir
            logger.exception("Disk cache unavailable, falling back to in-memory cache")
    litellm.cache = Cache(type="local", ttl=settings.cache_ttl_seconds)
    logger.info("LiteLLM in-memory cache enabled")


def parse_simulation_header(raw: str | None, pool: list[KeyConfig]) -> dict[str, int]:
    """Parse `key_1=429,key_2=500` or `*=429` into {key_id: status}. Invalid pairs are ignored."""
    if not raw:
        return {}
    key_ids = [k.key_id for k in pool]
    wildcard: int | None = None
    specific: dict[str, int] = {}
    for pair in raw.split(","):
        if "=" not in pair:
            continue
        name, _, status_raw = pair.partition("=")
        name, status_raw = name.strip(), status_raw.strip()
        try:
            status = int(status_raw)
        except ValueError:
            continue
        if status not in SIMULATABLE_STATUSES:
            continue
        if name == "*":
            wildcard = status
        elif name in key_ids:
            specific[name] = status
    result = {key_id: wildcard for key_id in key_ids} if wildcard is not None else {}
    result.update(specific)
    return result


def simulated_provider_error(status: int, key: KeyConfig) -> Exception:
    """Build the same typed exception LiteLLM raises when a provider returns `status`."""
    provider = key.model.split("/", 1)[0] if "/" in key.model else "openai"
    message = f"[simulated] provider returned HTTP {status} for {key.key_id}"
    if status == 429:
        return litellm.RateLimitError(message=message, llm_provider=provider, model=key.model)
    if status == 503:
        return litellm.ServiceUnavailableError(message=message, llm_provider=provider, model=key.model)
    if status == 500:
        return litellm.InternalServerError(message=message, llm_provider=provider, model=key.model)
    return litellm.APIError(status_code=status, message=message, llm_provider=provider, model=key.model)


def _status_of(exc: Exception) -> int | str:
    return getattr(exc, "status_code", None) or type(exc).__name__


class LLMGateway:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.pool = settings.keys

    async def complete(self, messages: list[dict[str, str]], simulate: dict[str, int] | None = None) -> CompletionResult:
        simulate = simulate or {}
        # When a failure is being simulated we skip the cache *read* so the request
        # really exercises the key pool (otherwise a cached answer would hide the
        # failover). The fresh answer is still written to the cache.
        bypass_cache_read = bool(simulate)
        failures = 0

        for key in self.pool:
            try:
                if key.key_id in simulate:
                    raise simulated_provider_error(simulate[key.key_id], key)

                response = await litellm.acompletion(
                    model=key.model,
                    messages=messages,
                    api_key=key.secret,
                    temperature=self.settings.temperature,
                    max_tokens=self.settings.max_output_tokens,
                    timeout=self.settings.llm_timeout_seconds,
                    num_retries=0,  # we own retry/failover across keys
                    caching=self.settings.cache_enabled,
                    cache={"no-cache": bypass_cache_read},
                )
            except Exception as exc:  # any provider failure -> next key
                failures += 1
                logger.warning(
                    "key %s failed (%s: %s); %s",
                    key.key_id,
                    type(exc).__name__,
                    _status_of(exc),
                    "trying next key" if failures < len(self.pool) else "no keys left",
                )
                continue

            reply = _extract_text(response)
            if not reply:
                failures += 1
                logger.warning("key %s returned an empty reply; trying next key", key.key_id)
                continue

            cache_hit = bool((getattr(response, "_hidden_params", None) or {}).get("cache_hit"))
            usage = getattr(response, "usage", None)
            return CompletionResult(
                reply=reply,
                resolution_source=LLM_CACHE if cache_hit else LIVE_INFERENCE,
                model=key.model,
                input_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
                output_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
                key_id=None if cache_hit else key.key_id,
                fallback_attempts=0 if cache_hit else failures,
            )

        raise AllProvidersUnavailable()


def _extract_text(response: Any) -> str:
    try:
        content = response.choices[0].message.content
    except (AttributeError, IndexError, KeyError, TypeError):
        return ""
    return (content or "").strip()


def now_ms() -> float:
    return time.perf_counter() * 1000
