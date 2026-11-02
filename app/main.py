"""FastAPI application entry point.

Run locally with:

    uvicorn app.main:app --reload

In production see DEPLOYMENT.md (``scripts/start.sh`` applies migrations first).
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Union
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from prometheus_client import make_asgi_app
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from .api.ratelimit import limit_search
from .api.routes import (
    admin_router,
    chat_router,
    documents_router,
    leads_router,
    properties_router,
    voice_router,
    webhooks_router,
    whatsapp_gateway_router,
)
from .api.security import require_admin
from .helpers.config import get_settings
from .observability import configure_logging
from .observability.logging import request_id_var
from .workflows.rag import answer_query


configure_logging()
logger = logging.getLogger(__name__)

settings = get_settings()


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def _startup_database() -> None:
    from .agents.checkpointer import setup_checkpointer
    from .database.migrations import schema_status
    from .database.session import create_all, get_engine, is_postgres

    get_engine()
    if is_postgres():
        up_to_date, detail = schema_status()
        if not up_to_date:
            raise RuntimeError(f"Database schema is out of date - {detail}")
        setup_checkpointer()
    else:
        create_all()


def _ensure_vector_indexes() -> None:
    """Ensure Qdrant payload indexes exist on API startup.

    Idempotent: safe to call on every restart. Missing indexes are created
    automatically so filtered retrieval works consistently across channels.
    """
    from .database.vector_db import create_client, ensure_payload_indexes

    settings = get_settings()
    client = create_client()
    try:
        if not client.collection_exists(settings.QDRANT_COLLECTION):
            logger.warning(
                "Qdrant collection does not exist yet | collection=%s",
                settings.QDRANT_COLLECTION,
            )
            return
        results = ensure_payload_indexes(client, settings.QDRANT_COLLECTION)
        created = [f for f, r in results.items() if r == "created"]
        skipped = [f for f, r in results.items() if r == "already_exists"]
        failed = [f for f, r in results.items() if r == "failed"]
        if created:
            logger.info(
                "Payload indexes ensured at startup | collection=%s created=%s",
                settings.QDRANT_COLLECTION,
                ", ".join(created),
            )
        if skipped:
            logger.debug(
                "Payload indexes already present at startup | collection=%s skipped=%s",
                settings.QDRANT_COLLECTION,
                ", ".join(skipped),
            )
        if failed:
            logger.warning(
                "Payload indexes failed at startup | collection=%s failed=%s",
                settings.QDRANT_COLLECTION,
                ", ".join(failed),
            )
    finally:
        client.close()


async def _maintenance_loop() -> None:
    """Retry undelivered staff alerts and tidy expired guard rows."""
    from .database.replay import purge_expired
    from .notifications.staff import retry_pending

    tick = 0
    while True:
        await asyncio.sleep(60)
        tick += 1
        try:
            await run_in_threadpool(retry_pending)
            if tick % 60 == 0:
                await run_in_threadpool(purge_expired)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Maintenance tick failed")


@asynccontextmanager
async def lifespan(_: FastAPI):
    current = get_settings()
    if current.is_production:
        problems = current.production_problems()
        if problems:
            for problem in problems:
                logger.error("Configuration error: %s", problem)
            raise RuntimeError(f"Invalid production configuration ({len(problems)} problem(s)); see log")
    for warning in current.production_warnings():
        logger.warning("Configuration: %s", warning)

    await run_in_threadpool(_startup_database)
    await run_in_threadpool(_ensure_vector_indexes)
    maintenance = asyncio.create_task(_maintenance_loop())
    logger.info("Startup complete | env=%s", current.APP_ENV)
    try:
        yield
    finally:
        maintenance.cancel()
        from .agents.checkpointer import close_checkpointer
        from .database.session import reset_engine

        close_checkpointer()
        reset_engine()


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Real Estate Assistant API",
    version="0.1.0",
    description="API for the real-estate assistant.",
    lifespan=lifespan,
    # Interactive docs reveal every route and schema; production serves none.
    docs_url="/docs" if settings.docs_enabled else None,
    redoc_url="/redoc" if settings.docs_enabled else None,
    openapi_url="/openapi.json" if settings.docs_enabled else None,
)


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------

# CORS_ALLOWED_ORIGINS defaults to "*" for local development; production
# refuses to start with it (also enforced on the voice socket).
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "Accept"],
    expose_headers=["Retry-After", "X-Request-ID"],
    max_age=600,
)


@app.middleware("http")
async def request_context(request: Request, call_next):
    """Correlate log lines per request and add baseline security headers.

    The path is logged without its query string: the voice WebSocket token and
    other credentials travel there.
    """
    request_id = uuid4().hex[:12]
    token = request_id_var.set(request_id)
    started = time.monotonic()
    try:
        response: Response = await call_next(request)
    except Exception:
        logger.exception("Unhandled error | %s %s", request.method, request.url.path)
        response = JSONResponse({"detail": "Internal server error"}, status_code=500)
    finally:
        request_id_var.reset(token)
    response.headers["X-Request-ID"] = request_id
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("X-Frame-Options", "DENY")
    if get_settings().is_production:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    if request.url.path not in {"/health", "/metrics"}:
        logger.info(
            "%s %s -> %s (%.0f ms)",
            request.method, request.url.path, response.status_code, (time.monotonic() - started) * 1000,
        )
    return response


# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------

app.include_router(voice_router)
app.include_router(webhooks_router)
app.include_router(whatsapp_gateway_router)
app.include_router(chat_router)
app.include_router(properties_router)
app.include_router(documents_router)
app.include_router(leads_router)
app.include_router(admin_router)


# ---------------------------------------------------------------------------
# Frontend (local development / single-host deployments)
# ---------------------------------------------------------------------------

FRONTEND_DIR = Path(__file__).parent / "frontend"


def _metrics_app():
    inner = make_asgi_app()

    async def guarded(scope, receive, send):
        # Metrics expose request volumes and error rates: operators only.
        if scope["type"] == "http" and not get_settings().metrics_public:
            from fastapi import Request as _Request

            request = _Request(scope, receive)
            try:
                require_admin(request, request.headers.get("x-admin-key"))
            except HTTPException as error:
                response = JSONResponse({"detail": error.detail}, status_code=error.status_code, headers=error.headers)
                await response(scope, receive, send)
                return
        await inner(scope, receive, send)

    return guarded


app.mount("/metrics", _metrics_app())

if FRONTEND_DIR.exists():
    app.mount(
        "/assets",
        StaticFiles(directory=FRONTEND_DIR),
        name="frontend-assets",
    )


# ---------------------------------------------------------------------------
# Request schema
# ---------------------------------------------------------------------------

RequirementValue = Union[
    str,
    int,
    float,
    bool,
    List[str],
    None,
]


class RAGRequest(BaseModel):
    """Customer query and extracted property requirements."""

    query: str = Field(
        min_length=1,
        max_length=2000,
    )

    requirements: Dict[str, RequirementValue] = Field(
        default_factory=dict,
    )

    # Number of final properties returned to the customer / LLM.
    top_k: int = Field(
        default=5,
        ge=1,
        le=20,
    )

    # Number of candidates retrieved before reranking.
    retrieval_top_k: int = Field(
        default=20,
        ge=1,
        le=100,
    )


# ---------------------------------------------------------------------------
# RAG endpoint
# ---------------------------------------------------------------------------

@app.post("/rag/search", tags=["rag"])
async def rag_search(request: RAGRequest, http_request: Request) -> dict[str, Any]:
    """
    Run the complete RAG retrieval and generation pipeline.

    Pipeline:

        Customer Query
            ↓
        Requirements
            ↓
        Metadata Filter
            ↓
        Dense / Hybrid Retrieval
            ↓
        Top-N Candidates
            ↓
        Reranking
            ↓
        Final Top-K
            ↓
        Grounded Generation
    """

    limit_search(http_request)
    try:
        return await run_in_threadpool(
            answer_query,
            request.query,
            request.requirements,
            top_k=request.top_k,
            retrieval_top_k=request.retrieval_top_k,
        )
    except Exception as error:
        logger.exception("RAG search failed")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="RAG workflow is unavailable",
        ) from error


# ---------------------------------------------------------------------------
# Frontend route
# ---------------------------------------------------------------------------

@app.get("/", include_in_schema=False)
async def website() -> FileResponse:
    """Serve the public real-estate website."""

    index_file = FRONTEND_DIR / "index.html"

    if not index_file.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Frontend is not available",
        )

    return FileResponse(index_file)


# ---------------------------------------------------------------------------
# Health checks
# ---------------------------------------------------------------------------

@app.get("/health", tags=["system"])
async def health() -> dict[str, str]:
    """Liveness only: no dependencies are touched, so it is safe to poll often
    (uptime monitors, the host's health check)."""

    return {"status": "ok", "name": app.title, "version": app.version}


_ready_cache: dict[str, Any] = {"at": 0.0, "body": None, "ok": False}
_READY_TTL_SECONDS = 20


def _readiness_report() -> tuple[bool, dict[str, Any]]:
    """Check every dependency the assistant needs. Never includes secrets."""
    from .agents.checkpointer import checkpointer_ready
    from .database.migrations import schema_status
    from .database.session import check_database, is_postgres
    from .database.vector_db import describe_collection

    current = get_settings()
    checks: dict[str, Any] = {}

    db_ok, db_error = check_database()
    checks["database"] = {"ok": db_ok, "engine": "postgresql" if is_postgres() else "sqlite", **({"error": db_error} if db_error else {})}
    if db_ok:
        migrated, detail = schema_status()
        checks["migrations"] = {"ok": migrated, "detail": detail}
        memory_ok, memory_error = checkpointer_ready()
        checks["conversation_memory"] = {"ok": memory_ok, **({"error": memory_error} if memory_error else {})}
    vector = describe_collection()
    checks["vector_search"] = vector
    checks["providers"] = {
        "ok": bool(current.GROQ_API_KEY),
        "groq": bool(current.GROQ_API_KEY),
        "gemini_embeddings": current.EMBEDDING_BACKEND != "gemini" or bool(current.GEMINI_API_KEY),
        "gemini_voice": bool(current.GEMINI_API_KEY),
        "whatsapp": current.whatsapp_configured,
        "staff_alerts": current.staff_channels,
    }
    critical = ("database", "migrations", "conversation_memory", "vector_search", "providers")
    ready = all(checks[name]["ok"] for name in critical if name in checks)
    return ready, {"status": "ready" if ready else "degraded", "environment": current.APP_ENV, "checks": checks}


@app.get("/health/ready", tags=["system"])
async def readiness() -> JSONResponse:
    """Verify the database, conversation memory, vector index and providers.

    Cached for a few seconds: probes reach Neon and Qdrant, which would
    otherwise wake the database and burn quota on every poll.
    """
    now = time.monotonic()
    if _ready_cache["body"] is None or now - _ready_cache["at"] > _READY_TTL_SECONDS:
        ok, body = await run_in_threadpool(_readiness_report)
        _ready_cache.update(at=now, body=body, ok=ok)
    return JSONResponse(_ready_cache["body"], status_code=200 if _ready_cache["ok"] else 503)
