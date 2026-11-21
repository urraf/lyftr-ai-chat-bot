import sys
from pathlib import Path

import litellm
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings, build_key_pool  # noqa: E402
from app.main import create_app  # noqa: E402

_real_acompletion = litellm.acompletion


class FakeProvider:
    """Routes calls through the real litellm.acompletion (so LiteLLM's cache runs for real)
    but swaps the network call for `mock_response`. Keys listed in `failing` raise the
    exception LiteLLM would raise for that provider status."""

    def __init__(self):
        self.failing: dict[str, Exception] = {}
        self.calls: list[str] = []  # api keys that actually reached the "provider"

    async def __call__(self, **kwargs):
        api_key = kwargs.get("api_key")
        if api_key in self.failing:
            self.calls.append(api_key)
            raise self.failing[api_key]
        last = kwargs["messages"][-1]["content"]
        kwargs["mock_response"] = f"echo: {last} (turns={len(kwargs['messages'])})"
        response = await _real_acompletion(**kwargs)
        if not (getattr(response, "_hidden_params", {}) or {}).get("cache_hit"):
            self.calls.append(api_key)
        return response


@pytest.fixture
def provider(monkeypatch):
    fake = FakeProvider()
    monkeypatch.setattr(litellm, "acompletion", fake)
    return fake


@pytest.fixture
def make_client(tmp_path, provider):
    def _make(simulation: bool = True, keys: str = "sk-one,sk-two", cache_type: str = "local"):
        settings = Settings(
            keys=build_key_pool(keys, "groq/openai/gpt-oss-20b", None),
            database_path=str(tmp_path / "test.db"),
            allowed_origins=["http://localhost:3000"],
            enable_failure_simulation=simulation,
            cache_type=cache_type,
            cache_dir=str(tmp_path / "cache"),
        )
        return TestClient(create_app(settings))

    return _make


@pytest.fixture
def client(make_client):
    with make_client() as c:
        yield c
