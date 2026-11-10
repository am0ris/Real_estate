"""Application settings loaded from environment variables and .env."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0", "qdrant", "postgres"}


class Settings(BaseSettings):
    """Application configuration with safe defaults for local development."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # -----------------------------------------------------------------------
    # Application
    # -----------------------------------------------------------------------

    APP_NAME: str = "Real Estate Assistant API"
    APP_VERSION: str = "0.1.0"
    PRIMARY_LANG: str = "en"
    DEFAULT_LANG: str = "en"

    # "production" turns on fail-fast validation (see production_problems()).
    APP_ENV: Literal["development", "test", "production"] = "development"
    LOG_LEVEL: str = "INFO"
    # "json" (one object per line, for Render/log drains) or "text".
    LOG_FORMAT: Literal["json", "text"] = "text"

    # Interactive API docs and /openapi.json. Off in production unless enabled.
    ENABLE_API_DOCS: bool | None = None
    # Serve /metrics without credentials. Off in production (admin key needed).
    METRICS_PUBLIC: bool | None = None

    # Comma-separated browser origins allowed for CORS and the voice
    # WebSocket. "*" keeps local development open; set real origins in prod.
    CORS_ALLOWED_ORIGINS: str = "*"

    # HMAC key for website session ids and voice tokens. When unset a random
    # per-process key is used, so sessions do not survive a restart.
    SESSION_SECRET: str | None = None
    # A website session id stops being accepted this long after it was issued.
    SESSION_MAX_AGE_DAYS: int = 30

    # Required (X-Admin-Key header) for operator endpoints: property writes,
    # document ingestion, booking/handoff management. Unset = disabled.
    ADMIN_API_KEY: str | None = None

    # CRM workbook (app/crm/excel). CRM_API_KEY, when set, is what the sales team
    # sends as X-Admin-Key on /admin/crm/* instead of ADMIN_API_KEY, so they never
    # hold the key that manages listings and documents.
    CRM_API_KEY: str | None = None
    # Signs the workbook's hidden baseline so a tampered or foreign file is
    # refused on import. Unset = derived from ADMIN_API_KEY.
    CRM_SIGNING_KEY: str | None = None
    # Comma-separated sales agent names offered in the workbook dropdown.
    CRM_AGENTS: str = ""
    # The workbook is built in memory. Measured: 5,000 leads + 10,000 interactions
    # add ~90 MB and take ~3 s; 20,000 + 50,000 reach ~530 MB, which a 512 MB host
    # cannot hold. Raise these only together with the instance's memory.
    CRM_EXPORT_MAX_LEADS: int = 5_000
    CRM_EXPORT_MAX_INTERACTIONS: int = 10_000
    CRM_IMPORT_MAX_MB: int = 10
    CRM_IMPORT_MAX_ROWS: int = 10_000
    # A lead with no customer message for this many days gets a re-engage task.
    CRM_INACTIVE_DAYS: int = 7

    # -----------------------------------------------------------------------
    # Rate limiting (per process; see app/api/ratelimit.py)
    # -----------------------------------------------------------------------

    RATE_LIMIT_ENABLED: bool = True
    # Number of trusted reverse proxies in front of the app (Render = 1).
    # 0 ignores X-Forwarded-For and keys limits by the socket peer address.
    TRUSTED_PROXY_HOPS: int = 0
    RATE_LIMIT_CHAT_PER_MINUTE: int = 20
    RATE_LIMIT_CHAT_SESSION_PER_MINUTE: int = 12
    RATE_LIMIT_SESSION_CREATE_PER_HOUR: int = 30
    RATE_LIMIT_SEARCH_PER_MINUTE: int = 15
    RATE_LIMIT_VOICE_TOKEN_PER_MINUTE: int = 6
    RATE_LIMIT_VOICE_CONNECT_PER_MINUTE: int = 10
    RATE_LIMIT_LEADS_PER_HOUR: int = 10
    RATE_LIMIT_ADMIN_PER_MINUTE: int = 60
    # Wrong admin keys allowed per client per 15 minutes before a lockout.
    RATE_LIMIT_ADMIN_FAILURES_PER_15MIN: int = 8
    RATE_LIMIT_WHATSAPP_SENDER_PER_MINUTE: int = 15
    RATE_LIMIT_WEBHOOK_PER_MINUTE: int = 600

    # -----------------------------------------------------------------------
    # Voice (website, Gemini Live)
    # -----------------------------------------------------------------------

    VOICE_TOKEN_TTL_SECONDS: int = 60
    VOICE_MAX_SESSION_SECONDS: int = 600
    VOICE_IDLE_TIMEOUT_SECONDS: int = 90
    VOICE_MAX_CONCURRENT_SESSIONS: int = 20
    VOICE_MAX_FRAME_BYTES: int = 64_000
    # How long one agent (tool) turn may run before the voice reply falls back.
    VOICE_AGENT_TIMEOUT_SECONDS: int = 30
    # Server-side reconnects to Gemini Live per call (browser socket stays open).
    VOICE_MAX_PROVIDER_RECONNECTS: int = 3
    VOICE_RECONNECT_BACKOFF_SECONDS: float = 0.6
    # Simultaneous voice calls one client address may hold open.
    VOICE_MAX_SESSIONS_PER_IP: int = 3

    # -----------------------------------------------------------------------
    # Scheduling
    # -----------------------------------------------------------------------

    VIEWING_TIMEZONE: str = "Africa/Cairo"
    VIEWING_DAY_START_HOUR: int = 10
    VIEWING_DAY_END_HOUR: int = 18
    VIEWING_SLOT_MINUTES: int = 60
    # Customers must book at least this far ahead (advisor notice).
    BOOKING_MIN_NOTICE_HOURS: int = 1
    # A viewing request nobody confirms is released after this many hours.
    BOOKING_HOLD_TTL_HOURS: int = 24

    # -----------------------------------------------------------------------
    # Demo listings
    # -----------------------------------------------------------------------
    # Listings ingested from a file whose name contains one of DEMO_SOURCE_MARKERS
    # (test_properties.csv, demo_*.xlsx, ...) are TEST data, not inventory. They are
    # hidden from the public catalogue and from the assistant's answers unless this is
    # switched on explicitly; when it is, every such listing is returned with
    # ``is_demo: true`` so the website labels it as a demo. Real listings are unaffected.
    SHOW_DEMO_LISTINGS: bool = False
    DEMO_SOURCE_MARKERS: str = "test_,demo,sample"

    # -----------------------------------------------------------------------
    # Staff notifications (any combination; all optional)
    # -----------------------------------------------------------------------

    # Slack/Discord/Teams-compatible incoming webhook (JSON POST).
    STAFF_WEBHOOK_URL: str | None = None
    TELEGRAM_BOT_TOKEN: str | None = None
    TELEGRAM_CHAT_ID: str | None = None
    # Plain-text WhatsApp alert to the sales team (inside Meta's 24h window).
    STAFF_WHATSAPP_NUMBER: str | None = None
    SMTP_HOST: str | None = None
    SMTP_PORT: int = 587
    SMTP_USERNAME: str | None = None
    SMTP_PASSWORD: str | None = None
    SMTP_FROM: str | None = None
    STAFF_EMAIL_TO: str | None = None

    # -----------------------------------------------------------------------
    # Generation / LLM
    # -----------------------------------------------------------------------

    GENERATION_BACKEND: str = "groq"
    GENERATION_MODEL: str = "openai/gpt-oss-120b"

    GROQ_API_KEY: str | None = None
    GROQ_MODEL: str = "openai/gpt-oss-120b"
    # gpt-oss models think before answering, and those hidden tokens count against max_tokens and
    # the provider quota. "low" answers sooner and cheaper. Unset = the provider default.
    GROQ_REASONING_EFFORT: Literal["low", "medium", "high"] | None = None
    # How one conversation turn is serialized. "process": a lock inside the API process (correct
    # for the single worker start.sh runs). "postgres": also an advisory lock on a pooled database
    # connection held for the WHOLE turn (needed only with several workers; caps concurrency at the
    # lock pool size).
    TURN_LOCK_BACKEND: Literal["process", "postgres"] = "process"

    OPENAI_API_KEY: str | None = None
    OPENAI_API_URL: str | None = None

    GEMINI_API_KEY: str | None = None
    GEMINI_LIVE_MODEL: str = "gemini-3.1-flash-live-preview"
    GEMINI_VOICE: str = "Aoede"

    # -----------------------------------------------------------------------
    # Embeddings
    # -----------------------------------------------------------------------

    # "gemini" (hosted, fits free tiers) or "local" (sentence-transformers,
    # needs requirements-ingest.txt and ~1 GB RAM).
    EMBEDDING_BACKEND: Literal["gemini", "local"] = "gemini"
    EMBEDDING_MODEL: str = "gemini-embedding-001"
    EMBEDDING_MODEL_ID: str | None = None
    # Must match the Qdrant collection's vector size. Changing it requires
    # re-indexing; the app refuses to search a collection of another size.
    EMBEDDING_DIMENSION: int = 768
    EMBEDDING_LOCAL_MODEL: str = "intfloat/multilingual-e5-small"

    # -----------------------------------------------------------------------
    # Reranking
    # -----------------------------------------------------------------------

    # "groq" (LLM listwise rerank, hosted), "local" (cross-encoder) or
    # "none" (keep the hybrid-search order).
    RERANKER_BACKEND: Literal["groq", "local", "none"] = "groq"
    RERANKER_MODEL: str = "BAAI/bge-reranker-v2-m3"
    RERANKER_DEVICE: str | None = None
    RERANKER_GROQ_MODEL: str | None = None

    # -----------------------------------------------------------------------
    # Qdrant
    # -----------------------------------------------------------------------

    QDRANT_CLUSTER_ENDPOINT: str | None = None
    QDRANT_URL: str | None = None
    QDRANT_API_KEY: str | None = None
    QDRANT_COLLECTION: str = "properties"

    # -----------------------------------------------------------------------
    # Other retrieval / AI providers
    # -----------------------------------------------------------------------

    COHERE_API_KEY: str | None = None

    # -----------------------------------------------------------------------
    # WhatsApp
    # -----------------------------------------------------------------------

    WHATSAPP_VERIFY_TOKEN: str | None = None
    WHATSAPP_APP_SECRET: str | None = None
    WHATSAPP_ACCESS_TOKEN: str | None = None
    WHATSAPP_PHONE_NUMBER_ID: str | None = None
    WHATSAPP_API_VERSION: str = "v23.0"
    WHATSAPP_TRANSCRIPTION_MODEL: str = "whisper-large-v3-turbo"
    # Shared bearer secret for the optional open-wa gateway (channels/whatsapp.ts).
    WHATSAPP_GATEWAY_SECRET: str | None = None

    # -----------------------------------------------------------------------
    # PostgreSQL
    # -----------------------------------------------------------------------

    # Full connection string (Neon: use the *direct* host, with sslmode=require).
    # Takes precedence over the POSTGRES_* parts below.
    DATABASE_URL: str | None = None
    POSTGRES_USERNAME: str | None = None
    POSTGRES_PASSWORD: str | None = None
    POSTGRES_HOST: str | None = None
    POSTGRES_PORT: int | None = None
    POSTGRES_MAIN_DATABASE: str | None = None
    DATABASE_POOL_SIZE: int = 5
    DATABASE_MAX_OVERFLOW: int = 5

    # -----------------------------------------------------------------------
    # Vector database
    # -----------------------------------------------------------------------

    VECTOR_DB_BACKEND: str = "qdrant"
    VECTOR_DB_PATH: str | None = None
    VECTOR_DB_DISTANCE_METHOD: str = "cosine"

    # -----------------------------------------------------------------------
    # File / ingestion settings
    # -----------------------------------------------------------------------

    FILE_ALLOWED_TYPES: list[str] = Field(default_factory=list)
    FILE_MAX_SIZE: int | None = None
    FILE_DEFAULT_CHUNK_SIZE: int = 1500
    FILE_CHUNK_OVERLAP: int = 100

    @field_validator("FILE_MAX_SIZE", "POSTGRES_PORT", "ENABLE_API_DOCS", "METRICS_PUBLIC", mode="before")
    @classmethod
    def blank_is_unset(cls, value: object) -> object:
        # ".env.example" ships blank optional values ("FILE_MAX_SIZE=").
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator(
        "DATABASE_URL", "SESSION_SECRET", "ADMIN_API_KEY", "CRM_API_KEY", "CRM_SIGNING_KEY",
        "QDRANT_URL", "QDRANT_API_KEY",
        "GROQ_API_KEY", "GEMINI_API_KEY", "WHATSAPP_VERIFY_TOKEN", "WHATSAPP_APP_SECRET",
        "WHATSAPP_ACCESS_TOKEN", "WHATSAPP_PHONE_NUMBER_ID", "WHATSAPP_GATEWAY_SECRET",
        "STAFF_WEBHOOK_URL", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "STAFF_WHATSAPP_NUMBER",
        "SMTP_HOST", "SMTP_USERNAME", "SMTP_PASSWORD", "SMTP_FROM", "STAFF_EMAIL_TO",
        "POSTGRES_USERNAME", "POSTGRES_PASSWORD", "POSTGRES_HOST", "POSTGRES_MAIN_DATABASE",
        mode="before",
    )
    @classmethod
    def strip_secret(cls, value: object) -> object:
        # Hosting dashboards make it easy to paste a trailing newline or quotes.
        if isinstance(value, str):
            value = value.strip().strip('"').strip("'").strip()
            return value or None
        return value

    # -----------------------------------------------------------------------
    # Derived values
    # -----------------------------------------------------------------------

    @property
    def is_production(self) -> bool:
        return self.APP_ENV == "production"

    @property
    def cors_origins(self) -> list[str]:
        return [origin.strip().rstrip("/") for origin in self.CORS_ALLOWED_ORIGINS.split(",") if origin.strip()]

    @property
    def demo_markers(self) -> list[str]:
        return [marker.strip().lower() for marker in self.DEMO_SOURCE_MARKERS.split(",") if marker.strip()]

    @property
    def docs_enabled(self) -> bool:
        return self.ENABLE_API_DOCS if self.ENABLE_API_DOCS is not None else not self.is_production

    @property
    def metrics_public(self) -> bool:
        return self.METRICS_PUBLIC if self.METRICS_PUBLIC is not None else not self.is_production

    @property
    def qdrant_endpoint(self) -> str:
        return self.QDRANT_CLUSTER_ENDPOINT or self.QDRANT_URL or "http://localhost:6333"

    @property
    def whatsapp_configured(self) -> bool:
        return bool(self.WHATSAPP_ACCESS_TOKEN and self.WHATSAPP_PHONE_NUMBER_ID and self.WHATSAPP_APP_SECRET)

    @property
    def crm_agents(self) -> list[str]:
        """Sales agent names from CRM_AGENTS, de-duplicated, order preserved."""
        names: list[str] = []
        for raw in self.CRM_AGENTS.split(","):
            name = " ".join(raw.split())[:80]
            if name and name.casefold() not in {existing.casefold() for existing in names}:
                names.append(name)
        return names

    @property
    def staff_channels(self) -> list[str]:
        channels = []
        if self.STAFF_WEBHOOK_URL:
            channels.append("webhook")
        if self.TELEGRAM_BOT_TOKEN and self.TELEGRAM_CHAT_ID:
            channels.append("telegram")
        if self.STAFF_WHATSAPP_NUMBER and self.whatsapp_configured:
            channels.append("whatsapp")
        if self.SMTP_HOST and self.SMTP_FROM and self.STAFF_EMAIL_TO:
            channels.append("email")
        return channels

    def production_problems(self) -> list[str]:
        """Return human-readable configuration errors; never includes secret values.

        An empty list means the process is safe to start with APP_ENV=production.
        """
        problems: list[str] = []

        def missing(name: str, why: str) -> None:
            problems.append(f"{name} is required: {why}")

        if not (self.DATABASE_URL or self.POSTGRES_HOST):
            missing("DATABASE_URL", "conversations, CRM and bookings are stored in PostgreSQL")
        if not self.SESSION_SECRET or len(self.SESSION_SECRET) < 32:
            missing("SESSION_SECRET", "at least 32 characters; signs website sessions and voice tokens")
        if not self.ADMIN_API_KEY or len(self.ADMIN_API_KEY) < 24:
            missing("ADMIN_API_KEY", "at least 24 characters; staff confirm bookings and handoffs with it")

        if self.CRM_API_KEY and len(self.CRM_API_KEY) < 24:
            problems.append("CRM_API_KEY must be at least 24 characters when set (it unlocks the lead export)")

        origins = self.cors_origins
        if not origins or "*" in origins:
            problems.append("CORS_ALLOWED_ORIGINS must list the real frontend origin(s), not '*'")
        else:
            for origin in origins:
                parsed = urlparse(origin)
                if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1"}:
                    problems.append(f"CORS_ALLOWED_ORIGINS entry {origin!r} must use https")

        if not self.GROQ_API_KEY:
            missing("GROQ_API_KEY", "grounded answer generation and the reranker")
        if self.EMBEDDING_BACKEND == "gemini" and not self.GEMINI_API_KEY:
            missing("GEMINI_API_KEY", "query/document embeddings (EMBEDDING_BACKEND=gemini)")

        qdrant_host = urlparse(self.qdrant_endpoint).hostname or ""
        if qdrant_host in _LOCAL_HOSTS:
            missing("QDRANT_URL", "point it at Qdrant Cloud, not a local instance")
        elif not self.QDRANT_API_KEY:
            missing("QDRANT_API_KEY", "Qdrant Cloud requires an API key")

        whatsapp_parts = {
            "WHATSAPP_VERIFY_TOKEN": self.WHATSAPP_VERIFY_TOKEN,
            "WHATSAPP_APP_SECRET": self.WHATSAPP_APP_SECRET,
            "WHATSAPP_ACCESS_TOKEN": self.WHATSAPP_ACCESS_TOKEN,
            "WHATSAPP_PHONE_NUMBER_ID": self.WHATSAPP_PHONE_NUMBER_ID,
        }
        if any(whatsapp_parts.values()):
            for name, value in whatsapp_parts.items():
                if not value:
                    missing(name, "WhatsApp is partially configured; set all four WHATSAPP_* values or none")

        if self.RATE_LIMIT_ENABLED and self.TRUSTED_PROXY_HOPS < 1:
            problems.append(
                "TRUSTED_PROXY_HOPS must be >= 1 behind Render's proxy, otherwise every client shares one rate-limit bucket"
            )
        return problems

    def production_warnings(self) -> list[str]:
        """Non-fatal gaps worth surfacing in the startup log."""
        warnings: list[str] = []
        if not self.GEMINI_API_KEY:
            warnings.append("GEMINI_API_KEY is not set: voice chat is disabled")
        if not self.staff_channels:
            warnings.append("no staff notification channel configured: booking/handoff alerts only reach the admin API")
        if not self.whatsapp_configured:
            warnings.append("WhatsApp is not configured: the WhatsApp channel is disabled")
        if self.SHOW_DEMO_LISTINGS:
            warnings.append("SHOW_DEMO_LISTINGS is on: test listings are shown to visitors, labelled as demo data")
        return warnings


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return one cached settings instance for the process."""
    return Settings()


def reset_settings_cache() -> None:
    """Drop the cached settings (tests that change environment variables)."""
    get_settings.cache_clear()
