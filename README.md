# LLM Chatbot with Key Failover and Response Caching

A small full-stack chat app: **React (Vite)** frontend, **FastAPI** backend, **LiteLLM** as the LLM gateway (Groq `llama-3.1-8b-instant` by default), and **SQLite** for session history.

Every assistant reply shows where it came from (`LIVE_INFERENCE` / `LLM_CACHE`), the model, server latency and the input/output tokens for that request only. If an API key is rate-limited, out of quota or returns a 5xx, the backend moves on to the next key, so the user never sees a broken screen.

```
React (nginx :3000) ──HTTP/JSON──► FastAPI (:8000) ──► LLMGateway ──► litellm.acompletion(key_1 → key_2 → …)
                                       │                              │
                                       ▼                              ▼
                                SQLite /data/app.db        LiteLLM cache (disk: /data/llm_cache)
```

---

## 1. Live URLs

| | URL |
|---|---|
| Frontend | `<FRONTEND_URL>` |
| Backend API base | `<BACKEND_URL>` (try `<BACKEND_URL>/health`) |
| Demo video | `<VIDEO_URL>` |

---

## 2. Run locally

### With Docker

```bash
cp .env.example .env        # then put 2+ real keys in LLM_API_KEYS (free Groq keys: https://console.groq.com/keys)
make up                     # backend http://localhost:8000, frontend http://localhost:3000
make down                   # stop (the SQLite volume is kept)
```

`docker-compose.yml` runs both services and mounts the named volume `app-data` at `/data`. That volume holds both `app.db` and the LiteLLM disk cache, so history and cached replies survive container restarts. Both Dockerfiles are multi-stage: the backend builds a venv on `python:3.12-slim` and copies only that venv and the app into a slim runtime image that runs as a non-root user. The frontend builds with `node:20-alpine` and is served as static files by `nginx:alpine`.

The frontend image reads `API_BASE_URL` at container start and writes it to `/config.js`, so the same image works against any backend without a rebuild.

### Without Docker

```bash
# backend (Python 3.11+)
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
export LLM_API_KEYS=gsk_key1,gsk_key2 DATABASE_URL=sqlite:///./app.db CACHE_DIR=./.llm_cache \
       ALLOWED_ORIGINS=http://localhost:3000 ENABLE_FAILURE_SIMULATION=true
uvicorn app.main:app --reload --port 8000
python -m pytest -q          # 36 tests, no network or real keys needed

# frontend
cd frontend
npm install
VITE_API_BASE_URL=http://localhost:8000 npm run dev    # http://localhost:3000
```

### Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `LLM_API_KEYS` | — | Comma-separated key pool (at least 2). Ids are `key_1`, `key_2`, … in this order |
| `LLM_MODEL` | `groq/llama-3.1-8b-instant` | LiteLLM model name used by every key |
| `LLM_MODELS` | — | Optional per-key models, same order as the keys (lets the pool span providers, e.g. Groq + Gemini) |
| `DATABASE_URL` | `sqlite:///./app.db` (`sqlite:////data/app.db` in Docker) | SQLite location |
| `ALLOWED_ORIGINS` | `http://localhost:3000,http://localhost:5173` | CORS origins, comma-separated |
| `ENABLE_FAILURE_SIMULATION` | `false` | `true` turns on the `X-Simulate-Failure` hook |
| `CACHE_TYPE` | `disk` | `disk` (persistent), `local` (in-memory) or `none` |
| `CACHE_DIR` / `CACHE_TTL_SECONDS` | `./.llm_cache` / `86400` | Disk cache location and TTL |
| `DEFAULT_SYSTEM_PROMPT` | helpful-assistant prompt | Used when a session has not set its own |
| `LLM_TIMEOUT_SECONDS`, `LLM_MAX_OUTPUT_TOKENS`, `LLM_TEMPERATURE`, `MAX_HISTORY_MESSAGES` | `30`, `1024`, `0.2`, `40` | Tuning |
| `API_BASE_URL` (frontend container) / `VITE_API_BASE_URL` (build) | `http://localhost:8000` | Backend the UI talks to |

---

## 3. Design decisions

### Key fallback

- **Key pool.** `LLM_API_KEYS` is parsed into an ordered pool of `{key_id, secret, model}`. The secret is kept out of `repr`, logs and responses; only `key_1`, `key_2`, … ever leave the process.
- **Ordered failover, with LiteLLM doing the provider work.** `LLMGateway.complete()` (`backend/app/llm.py`) walks the pool in order and calls `litellm.acompletion(model, messages, api_key=…)`. LiteLLM normalises every provider error into typed exceptions (`RateLimitError` for 429/quota, `InternalServerError`, `ServiceUnavailableError`, `APIError(502)`, `Timeout`, `APIConnectionError`, auth errors). Any of these moves the request on to the next key, and `fallback_attempts` counts how many keys failed first.
- **Why my own loop and not the LiteLLM Router's `fallbacks`?** The API contract needs the exact `key_id` that answered and an exact `fallback_attempts` count for every request. It also needs failures injected for one key and one request only (`X-Simulate-Failure`). With a short explicit loop those are deterministic and easy to test. LiteLLM still does the provider calls, error mapping and caching. `num_retries=0` stops LiteLLM from retrying the same key, so a dead key costs one round trip.
- **All keys failed** → `AllProvidersUnavailable` → `503 {"detail": "all providers unavailable"}`. Nothing is written to SQLite.
- **Failure simulation.** When `ENABLE_FAILURE_SIMULATION=true`, the header `X-Simulate-Failure: key_1=429,key_2=500` (or `*=429`) is parsed per request. A listed key raises the same LiteLLM exception type the provider would cause, so the request takes exactly the same failover code path as a real outage. Simulated requests skip the cache read, because otherwise a cached answer would hide the failover. They still write to the cache, and they always return a real model answer.
- **No crashes.** Validation errors return FastAPI's 422 shape, HTTP errors are JSON `{"detail": …}`, and a catch-all handler turns anything unexpected into `500 {"detail": "internal server error"}`. Stack traces are never returned.

### Caching

- **LiteLLM's built-in cache** (`litellm.cache = Cache(type="disk")`, which falls back to in-memory) is enabled at startup, and every call passes `caching=True`.
- **Cache key = model + the full message list** (system prompt + history + new message) + generation params, as LiteLLM computes it. The API key is **not** part of LiteLLM's cache key, so a reply generated by `key_2` can be served later no matter which key is healthy. One result of this design: the same first message in a new session is a cache hit (same system prompt + same single user message), while a different message, a different system prompt or a different history is always a miss.
- **Detection.** LiteLLM sets `response._hidden_params["cache_hit"]`. A hit is reported as `LLM_CACHE` with `key_id: null` and `fallback_attempts: 0`. The token counts are the usage stored with the cached response, and the reply text is byte-identical. A hit makes no network call, so it takes a few ms against hundreds of ms for a live call.
- **Disk over memory.** The cache lives in the same `/data` volume as SQLite, so it survives restarts and redeploys that keep the volume. SQLite remains the only database (no Redis or Postgres).

### SQLite session storage

- Plain `sqlite3` (stdlib) with WAL mode. The schema is created on startup (`CREATE TABLE IF NOT EXISTS`):
  - `messages(id, session_id, role, content, metadata JSON, created_at ISO-8601)`, the required model, plus an index on `(session_id, id)`.
  - `sessions(id, system_prompt, created_at, updated_at)`, which stores the session's system prompt so it sticks for later turns. This lets the client send only the new message, as the spec requires.
- **Write only on success.** The user message and the assistant reply are inserted in **one transaction** after the LLM call succeeds. A request that fails (422/503) never touches history, and a crash mid-request cannot leave an orphan user message.
- The history sent to the LLM is the most recent `MAX_HISTORY_MESSAGES` (40) messages, which keeps prompts bounded in long chats. `GET /sessions/{id}/messages` returns the full history, oldest first, without the system prompt.
- `latency_ms` is measured server-side from the start of the handler until the reply is ready, so it includes the history read and the LLM/cache call.

### Project layout

```
backend/
  app/config.py   env → Settings, key pool
  app/db.py       SQLite schema + queries
  app/llm.py      LiteLLM gateway: cache config, failover loop, failure simulation
  app/main.py     FastAPI routes, validation, error handlers, CORS
  tests/          36 pytest tests (real LiteLLM cache, provider stubbed via mock_response)
  Dockerfile      multi-stage, non-root, healthcheck
frontend/
  src/App.jsx     chat UI with data-testid contract
  src/api.js      API client + readable error messages
  Dockerfile      multi-stage node → nginx, runtime API_BASE_URL
docker-compose.yml, Makefile, .env.example, render.yaml
```

---

## 4. Verification

Set `B=<BACKEND_URL>` (or `http://localhost:8000`).

**Health**
```bash
curl -s $B/health
# {"status":"ok","keys_configured":2,"cache_enabled":true,...}
```

**Live inference, then a cache hit (same first message, new session)**
```bash
curl -s -X POST $B/chat -H 'Content-Type: application/json' \
  -d '{"session_id":"demo-a","message":"What is the capital of France?"}'
# metadata.resolution_source = LIVE_INFERENCE, key_id = key_1, latency ~ hundreds of ms

curl -s -X POST $B/chat -H 'Content-Type: application/json' \
  -d '{"session_id":"demo-b","message":"What is the capital of France?"}'
# metadata.resolution_source = LLM_CACHE, identical reply, key_id = null, latency a few ms
```

**Failover**
```bash
curl -s -X POST $B/chat -H 'Content-Type: application/json' -H 'X-Simulate-Failure: key_1=429' \
  -d '{"session_id":"demo-c","message":"Name a prime number."}'
# 200, key_id = key_2, fallback_attempts = 1

curl -s -X POST $B/chat -H 'Content-Type: application/json' -H 'X-Simulate-Failure: key_1=500' \
  -d '{"session_id":"demo-c","message":"Another one?"}'
# 200, key_id = key_2

curl -s -i -X POST $B/chat -H 'Content-Type: application/json' -H 'X-Simulate-Failure: *=429' \
  -d '{"session_id":"demo-d","message":"hello"}'
# 503 {"detail":"all providers unavailable"} – and GET /sessions/demo-d/messages is 404 (nothing stored)
```

**Multi-turn memory + history**
```bash
curl -s -X POST $B/chat -H 'Content-Type: application/json' -d '{"session_id":"demo-e","message":"My name is Ada."}'
curl -s -X POST $B/chat -H 'Content-Type: application/json' -d '{"session_id":"demo-e","message":"What is my name?"}'
curl -s $B/sessions/demo-e/messages
```

**Validation**
```bash
curl -s -X POST $B/chat -H 'Content-Type: application/json' -d '{"session_id":"x","message":"   "}'   # 422
curl -s -X POST $B/chat -H 'Content-Type: application/json' -d '{oops'                              # 422
curl -s $B/sessions/nope/messages                                                                    # 404
```

**In the UI.** Each assistant bubble shows chips for `meta-source`, `meta-model`, `meta-latency`, `meta-input-tokens` and `meta-output-tokens` (plus `key_id` and a `failover ×N` chip). Under **Settings** there is the session system prompt (`system-prompt-input`) and a **Failover demo** dropdown that sends `X-Simulate-Failure` with your messages, so failover and the 503 message can be shown without curl. Reload the page to start a new session, then send the same first message to see `LLM_CACHE`.

**Tests**
```bash
cd backend && python -m pytest -q
```
The tests cover the contract fields, cache hit/miss rules (including the system prompt being part of the key), per-request tokens, multi-turn history and session isolation, every 422 case, failover for 429/500/502/503 (both simulated and through real LiteLLM exceptions), the 503 that leaves history untouched, the simulation switch, and that secrets never leak.

---

## 5. Setup Used

- **Editor:** VS Code
- **AI tools:** Claude Code (Claude Opus) for scaffolding, implementation, tests and docs. I reviewed and verified everything against the assignment contract.

---

## 6. Bonus features

- **Failover demo control in the UI.** The dropdown sends `X-Simulate-Failure`, so the failover and the 503 path can be demonstrated live from the deployed frontend.
- **Persistent disk cache** in the same `/data` volume, so cache hits survive restarts.
- **Per-key models (`LLM_MODELS`).** The pool can span providers (e.g. a Groq key and a Gemini key), so one provider going down doesn't take the app with it.
- **Per-session system prompt** stored in SQLite and reused on later turns.
- **Backend health badge** in the UI (keys configured, cache on/off) and extra `/health` fields (`database`, `models`, `failure_simulation_enabled`).
- **Bounded context** (`MAX_HISTORY_MESSAGES`), so long chats don't grow prompts without limit.
- **Runtime-configurable frontend image** (`API_BASE_URL` → `/config.js`).
- **Hardening:** non-root backend container, Docker healthcheck, secrets redacted from `repr`/logs, and atomic history writes.
