"""FastAPI app: GET /health, POST /chat, GET /sessions/{session_id}/messages."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from .config import Settings, get_settings
from .db import Database
from .llm import AllProvidersUnavailable, LLMGateway, configure_litellm, now_ms, parse_simulation_header

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("app")

MAX_MESSAGE_CHARS = 4000
MAX_SYSTEM_PROMPT_CHARS = 4000


# ---------- schemas ----------

class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    session_id: StrictStr = Field(min_length=1, max_length=128)
    message: StrictStr
    system_prompt: Optional[StrictStr] = None

    @field_validator("session_id")
    @classmethod
    def session_id_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("session_id must not be blank")
        return value

    @field_validator("message")
    @classmethod
    def message_length(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("message must not be empty")
        if len(value) > MAX_MESSAGE_CHARS:
            raise ValueError(f"message must be at most {MAX_MESSAGE_CHARS} characters")
        return value

    @field_validator("system_prompt")
    @classmethod
    def system_prompt_length(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        value = value.strip()
        if len(value) > MAX_SYSTEM_PROMPT_CHARS:
            raise ValueError(f"system_prompt must be at most {MAX_SYSTEM_PROMPT_CHARS} characters")
        return value or None


class ChatMetadata(BaseModel):
    resolution_source: str
    model: str
    latency_ms: int
    input_tokens: int
    output_tokens: int
    key_id: Optional[str]
    fallback_attempts: int


class ChatResponse(BaseModel):
    session_id: str
    reply: str
    metadata: ChatMetadata


# ---------- app factory ----------

def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    db = Database(settings.database_path)
    gateway = LLMGateway(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        db.init_schema()
        configure_litellm(settings)
        logger.info(
            "started: %d key(s) %s, cache=%s, failure_simulation=%s, db=%s",
            len(settings.keys),
            [f"{k.key_id}:{k.model}" for k in settings.keys],
            settings.cache_type,
            settings.enable_failure_simulation,
            settings.database_path,
        )
        yield

    app = FastAPI(title="LLM Chatbot with Key Failover and Response Caching", version="1.0.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.db = db
    app.state.gateway = gateway

    origins = settings.allowed_origins or ["http://localhost:3000"]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials="*" not in origins,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )

    # ----- error handlers: always JSON with a `detail` field, never a stack trace -----

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_: Request, exc: StarletteHTTPException):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail}, headers=getattr(exc, "headers", None))

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError):
        errors = []
        for err in exc.errors():
            errors.append({k: v for k, v in err.items() if k in {"type", "loc", "msg"}})
        return JSONResponse(status_code=422, content={"detail": errors})

    @app.exception_handler(Exception)
    async def unhandled_error(_: Request, exc: Exception):
        logger.exception("unhandled error: %s", type(exc).__name__)
        return JSONResponse(status_code=500, content={"detail": "internal server error"})

    # ----- routes -----

    @app.get("/health")
    async def health():
        db_ok = await run_in_threadpool(db.ping)
        keys = len(settings.keys)
        body = {
            "status": "ok" if db_ok and keys > 0 else "degraded",
            "keys_configured": keys,
            "cache_enabled": settings.cache_enabled,
            "database": "ok" if db_ok else "unreachable",
            "failure_simulation_enabled": settings.enable_failure_simulation,
            "models": sorted({k.model for k in settings.keys}),
        }
        return JSONResponse(status_code=200 if body["status"] == "ok" else 503, content=body)

    @app.post("/chat", response_model=ChatResponse)
    async def chat(
        payload: ChatRequest,
        x_simulate_failure: Optional[str] = Header(default=None, alias="X-Simulate-Failure"),
    ):
        started = now_ms()

        session = await run_in_threadpool(db.get_session, payload.session_id)
        stored_prompt = session["system_prompt"] if session else None
        system_prompt = payload.system_prompt or stored_prompt or settings.default_system_prompt

        history = await run_in_threadpool(db.get_history, payload.session_id, settings.max_history_messages)
        messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
        messages += [{"role": m["role"], "content": m["content"]} for m in history]
        messages.append({"role": "user", "content": payload.message})

        simulate = (
            parse_simulation_header(x_simulate_failure, settings.keys)
            if settings.enable_failure_simulation
            else {}
        )

        try:
            result = await gateway.complete(messages, simulate=simulate)
        except AllProvidersUnavailable:
            # Nothing is written to history for a failed request.
            raise HTTPException(status_code=503, detail="all providers unavailable")

        metadata = ChatMetadata(
            resolution_source=result.resolution_source,
            model=result.model,
            latency_ms=max(0, int(round(now_ms() - started))),
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            key_id=result.key_id,
            fallback_attempts=result.fallback_attempts,
        )

        await run_in_threadpool(
            db.save_turn,
            payload.session_id,
            payload.system_prompt or stored_prompt,
            payload.message,
            result.reply,
            metadata.model_dump(),
        )

        return ChatResponse(session_id=payload.session_id, reply=result.reply, metadata=metadata)

    @app.get("/sessions/{session_id}/messages")
    async def session_messages(session_id: str):
        if len(session_id) > 128 or not await run_in_threadpool(db.session_exists, session_id):
            raise HTTPException(status_code=404, detail="session not found")
        history = await run_in_threadpool(db.get_history, session_id)
        return {"session_id": session_id, "messages": history}

    return app


app = create_app()
