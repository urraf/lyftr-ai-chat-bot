import uuid

import litellm
import pytest

from app.config import build_key_pool, parse_database_url
from app.llm import parse_simulation_header


def sid() -> str:
    return f"s-{uuid.uuid4().hex[:8]}"


def chat(client, session_id, message, headers=None, **extra):
    return client.post("/chat", json={"session_id": session_id, "message": message, **extra}, headers=headers or {})


# ---------- /health ----------

def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["keys_configured"] == 2
    assert body["cache_enabled"] is True


# ---------- /chat contract ----------

def test_chat_live_inference_contract(client):
    s = sid()
    r = chat(client, s, f"What is the capital of France? {s}")
    assert r.status_code == 200
    body = r.json()
    assert body["session_id"] == s
    assert body["reply"]
    meta = body["metadata"]
    assert meta["resolution_source"] == "LIVE_INFERENCE"
    assert meta["model"] == "groq/openai/gpt-oss-20b"
    assert isinstance(meta["latency_ms"], int)
    assert isinstance(meta["input_tokens"], int) and meta["input_tokens"] > 0
    assert isinstance(meta["output_tokens"], int) and meta["output_tokens"] > 0
    assert meta["key_id"] == "key_1"
    assert meta["fallback_attempts"] == 0


def test_cache_hit_on_same_first_message_in_new_session(client, provider):
    msg = f"Tell me a fact {uuid.uuid4()}"
    first = chat(client, sid(), msg).json()
    second = chat(client, sid(), msg).json()
    assert first["metadata"]["resolution_source"] == "LIVE_INFERENCE"
    assert second["metadata"]["resolution_source"] == "LLM_CACHE"
    assert second["reply"] == first["reply"]
    assert second["metadata"]["key_id"] is None
    assert second["metadata"]["fallback_attempts"] == 0
    assert second["metadata"]["input_tokens"] == first["metadata"]["input_tokens"]
    assert second["metadata"]["output_tokens"] == first["metadata"]["output_tokens"]
    assert len(provider.calls) == 1  # the provider was only hit once


def test_different_message_never_from_cache(client):
    chat(client, sid(), f"alpha {uuid.uuid4()}")
    r = chat(client, sid(), f"beta {uuid.uuid4()}").json()
    assert r["metadata"]["resolution_source"] == "LIVE_INFERENCE"


def test_different_system_prompt_is_a_different_cache_key(client):
    msg = f"hello {uuid.uuid4()}"
    chat(client, sid(), msg)
    r = chat(client, sid(), msg, system_prompt="You are a pirate.").json()
    assert r["metadata"]["resolution_source"] == "LIVE_INFERENCE"


def test_tokens_are_per_request_not_cumulative(client):
    a = chat(client, sid(), f"Question number one {uuid.uuid4().hex[:6]}").json()["metadata"]
    b = chat(client, sid(), f"Question number two {uuid.uuid4().hex[:6]}").json()["metadata"]
    assert abs(a["input_tokens"] - b["input_tokens"]) <= 2


# ---------- multi-turn + history ----------

def test_multi_turn_history_is_sent_and_stored(client):
    s = sid()
    chat(client, s, "My name is Ada.")
    r = chat(client, s, "What is my name?").json()
    # mock reply echoes the number of messages sent: system + user + assistant + user = 4
    assert "(turns=4)" in r["reply"]

    hist = client.get(f"/sessions/{s}/messages")
    assert hist.status_code == 200
    body = hist.json()
    assert body["session_id"] == s
    roles = [m["role"] for m in body["messages"]]
    assert roles == ["user", "assistant", "user", "assistant"]
    assert body["messages"][0]["content"] == "My name is Ada."
    assert "metadata" not in body["messages"][0]
    meta = body["messages"][1]["metadata"]
    assert meta["resolution_source"] in {"LIVE_INFERENCE", "LLM_CACHE"}
    assert "input_tokens" in meta and "output_tokens" in meta
    assert all(m["role"] != "system" for m in body["messages"])


def test_sessions_are_isolated(client):
    a, b = sid(), sid()
    chat(client, a, "message for A")
    chat(client, b, "message for B")
    msgs_a = client.get(f"/sessions/{a}/messages").json()["messages"]
    assert [m["content"] for m in msgs_a if m["role"] == "user"] == ["message for A"]


def test_unknown_session_is_404(client):
    r = client.get("/sessions/does-not-exist/messages")
    assert r.status_code == 404
    assert r.json() == {"detail": "session not found"}


def test_session_system_prompt_persists(client, provider, monkeypatch):
    s = sid()
    chat(client, s, "hi", system_prompt="You are a pirate.")
    seen = []
    original = provider.__call__

    async def spy(**kwargs):
        seen.append(kwargs["messages"][0])
        return await original(**kwargs)

    monkeypatch.setattr(litellm, "acompletion", spy)
    chat(client, s, "again")
    assert seen[0] == {"role": "system", "content": "You are a pirate."}


# ---------- validation (422) ----------

@pytest.mark.parametrize(
    "payload",
    [
        {"session_id": "s-1"},
        {"message": "hi"},
        {"session_id": "", "message": "hi"},
        {"session_id": "s-1", "message": ""},
        {"session_id": "s-1", "message": "    "},
        {"session_id": "s-1", "message": "x" * 4001},
        {"session_id": "x" * 129, "message": "hi"},
        {"session_id": "s-1", "message": 123},
    ],
)
def test_validation_errors(client, payload):
    r = client.post("/chat", json=payload)
    assert r.status_code == 422
    assert "detail" in r.json()


def test_message_length_counted_after_trim(client):
    r = chat(client, sid(), "  " + "x" * 4000 + "  ")
    assert r.status_code == 200


def test_malformed_json_is_422(client):
    r = client.post("/chat", content="{not json", headers={"Content-Type": "application/json"})
    assert r.status_code == 422
    assert "detail" in r.json()


def test_failed_validation_not_added_to_history(client):
    s = sid()
    client.post("/chat", json={"session_id": s, "message": ""})
    assert client.get(f"/sessions/{s}/messages").status_code == 404


# ---------- failover via X-Simulate-Failure ----------

@pytest.mark.parametrize("header", ["key_1=429", "key_1=500", "key_1=502", "key_1=503"])
def test_simulated_failure_falls_over_to_key_2(client, header):
    r = chat(client, sid(), f"failover {uuid.uuid4()}", headers={"X-Simulate-Failure": header})
    assert r.status_code == 200
    meta = r.json()["metadata"]
    assert meta["key_id"] == "key_2"
    assert meta["fallback_attempts"] == 1
    assert meta["resolution_source"] == "LIVE_INFERENCE"
    assert not r.json()["reply"].startswith("[simulated]")


def test_simulated_failure_bypasses_cache_read(client):
    msg = f"cached {uuid.uuid4()}"
    chat(client, sid(), msg)
    r = chat(client, sid(), msg, headers={"X-Simulate-Failure": "key_1=429"}).json()
    assert r["metadata"]["key_id"] == "key_2"
    assert r["metadata"]["resolution_source"] == "LIVE_INFERENCE"


@pytest.mark.parametrize("header", ["*=429", "key_1=429,key_2=500"])
def test_all_keys_failing_is_503_and_not_stored(client, header):
    s = sid()
    r = chat(client, s, "hello", headers={"X-Simulate-Failure": header})
    assert r.status_code == 503
    assert r.json() == {"detail": "all providers unavailable"}
    assert client.get(f"/sessions/{s}/messages").status_code == 404


def test_503_mid_session_does_not_touch_history(client):
    s = sid()
    chat(client, s, "first")
    chat(client, s, "second", headers={"X-Simulate-Failure": "*=503"})
    msgs = client.get(f"/sessions/{s}/messages").json()["messages"]
    assert len(msgs) == 2


def test_simulation_ignored_when_disabled(make_client):
    with make_client(simulation=False) as c:
        r = chat(c, sid(), f"x {uuid.uuid4()}", headers={"X-Simulate-Failure": "*=429"})
        assert r.status_code == 200
        assert r.json()["metadata"]["key_id"] == "key_1"


def test_real_provider_429_falls_over(client, provider):
    provider.failing["sk-one"] = litellm.RateLimitError(message="quota exceeded", llm_provider="groq", model="m")
    r = chat(client, sid(), f"real {uuid.uuid4()}")
    assert r.status_code == 200
    assert r.json()["metadata"]["key_id"] == "key_2"
    assert r.json()["metadata"]["fallback_attempts"] == 1


def test_real_provider_all_down_is_503(client, provider):
    provider.failing["sk-one"] = litellm.RateLimitError(message="429", llm_provider="groq", model="m")
    provider.failing["sk-two"] = litellm.ServiceUnavailableError(message="503", llm_provider="groq", model="m")
    r = chat(client, sid(), f"real {uuid.uuid4()}")
    assert r.status_code == 503
    assert r.json() == {"detail": "all providers unavailable"}


def test_key_secret_never_leaks(client):
    r = chat(client, sid(), f"x {uuid.uuid4()}", headers={"X-Simulate-Failure": "key_1=429"})
    assert "sk-one" not in r.text and "sk-two" not in r.text


def test_disk_cache_works(make_client):
    with make_client(cache_type="disk") as c:
        msg = f"disk {uuid.uuid4()}"
        chat(c, sid(), msg)
        r = chat(c, sid(), msg).json()
        assert r["metadata"]["resolution_source"] == "LLM_CACHE"


# ---------- unit helpers ----------

def test_parse_simulation_header():
    pool = build_key_pool("a,b,c", "m", None)
    assert parse_simulation_header("key_1=429", pool) == {"key_1": 429}
    assert parse_simulation_header("key_1=429,key_2=500", pool) == {"key_1": 429, "key_2": 500}
    assert parse_simulation_header("*=429", pool) == {"key_1": 429, "key_2": 429, "key_3": 429}
    assert parse_simulation_header("*=429,key_2=500", pool)["key_2"] == 500
    assert parse_simulation_header("key_9=429,key_1=418,junk", pool) == {}
    assert parse_simulation_header(None, pool) == {}


def test_key_pool_and_db_url():
    pool = build_key_pool("k1, k2", "groq/x", "groq/x,gemini/y")
    assert [(k.key_id, k.model) for k in pool] == [("key_1", "groq/x"), ("key_2", "gemini/y")]
    assert "k1" not in repr(pool)
    assert parse_database_url("sqlite:////data/app.db") == "/data/app.db"
    assert parse_database_url("sqlite:///./app.db") == "./app.db"
