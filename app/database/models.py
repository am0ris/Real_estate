"""SQLAlchemy ORM models for the real-estate assistant."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator


# ============================================================
# Base
# ============================================================


class Base(DeclarativeBase):
    """Base class for all SQLAlchemy ORM models."""

    pass


class UTCDateTime(TypeDecorator):
    """``timestamptz`` that always round-trips as an aware UTC datetime.

    SQLite drops time zones, so this normalises on the way in and out; it also
    rejects naive datetimes so a missing zone is a loud bug, not a silent shift.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("Naive datetime: timestamps must be timezone-aware")
        return value.astimezone(timezone.utc)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ============================================================
# Property
# ============================================================


class Property(Base):
    """Real-estate property stored in the application database."""

    __tablename__ = "properties"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    # External/source property identifier.
    property_id: Mapped[str] = mapped_column(
        String(100),
        unique=True,
        nullable=False,
        index=True,
    )

    property_type: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
        index=True,
    )

    price: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 2),
        nullable=True,
    )

    currency: Mapped[str | None] = mapped_column(
        String(10),
        nullable=True,
    )

    area: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 2),
        nullable=True,
    )

    area_unit: Mapped[str | None] = mapped_column(
        String(30),
        nullable=True,
    )

    bedrooms: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )

    bathrooms: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )

    location: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        index=True,
    )

    city: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
        index=True,
    )

    district: Mapped[str | None] = mapped_column(
        String(150),
        nullable=True,
        index=True,
    )

    neighborhood: Mapped[str | None] = mapped_column(
        String(150),
        nullable=True,
    )

    compound: Mapped[str | None] = mapped_column(
        String(150),
        nullable=True,
    )

    transaction_type: Mapped[str | None] = mapped_column(
        String(50),
        nullable=True,
        index=True,
    )

    payment_plan: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
    )

    availability: Mapped[str | None] = mapped_column(
        String(50),
        nullable=True,
    )

    finishing: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
    )

    delivery: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
    )

    amenities: Mapped[list[Any] | None] = mapped_column(
        JSON,
        nullable=True,
    )

    source: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )

    url: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
        index=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        server_default=func.now(),
        nullable=False,
    )

    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


# ============================================================
# Customer
# ============================================================


class Customer(Base):
    """Customer or prospective buyer/renter interacting with the assistant."""

    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    name: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )

    # Not unique: a customer's identity is their conversation (external_id), and
    # one person legitimately appears under several sessions with the same number.
    phone: Mapped[str | None] = mapped_column(
        String(50),
        nullable=True,
        index=True,
    )

    email: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        index=True,
    )

    preferred_language: Mapped[str] = mapped_column(
        String(10),
        default="en",
        nullable=False,
    )

    source: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
    )

    notes: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    # Stable external reference (API responses, staff messages).
    public_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        default=uuid4,
        unique=True,
        nullable=False,
    )

    # Conversation key of the channel this customer talks on: "web:<session>"
    # or "whatsapp:<phone>". Customers are never merged across channels by an
    # unverified phone number (that would let anyone read another person's data).
    external_id: Mapped[str | None] = mapped_column(
        String(120),
        unique=True,
        nullable=True,
        index=True,
    )

    # True only after the customer explicitly confirmed this exact number
    # (read-back on website/voice) or it came from the WhatsApp sender id.
    phone_verified: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default=text("false"),
        nullable=False,
    )

    phone_verified_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(),
        nullable=True,
    )

    # "readback" (customer confirmed) or "whatsapp_sender" (channel-verified).
    phone_verification_method: Mapped[str | None] = mapped_column(
        String(30),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        server_default=func.now(),
        nullable=False,
    )

    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    leads: Mapped[list["Lead"]] = relationship(
        "Lead",
        back_populates="customer",
        cascade="all, delete-orphan",
    )




# ============================================================
# Lead
# ============================================================


class Lead(Base):
    """Sales lead associated with a customer."""

    __tablename__ = "leads"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    status: Mapped[str] = mapped_column(
        String(50),
        default="new",
        nullable=False,
        index=True,
    )

    intent: Mapped[str | None] = mapped_column(
        String(50),
        nullable=True,
    )

    budget_min: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 2),
        nullable=True,
    )

    budget_max: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 2),
        nullable=True,
    )

    preferred_location: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )

    preferred_property_type: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
    )

    bedrooms_min: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )

    notes: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    # SQLAlchemy reserves the attribute name "metadata".
    # Use "metadata_json" in Python while keeping the database
    # column name as "metadata".
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(
        "metadata",
        JSON,
        nullable=True,
    )

    public_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        default=uuid4,
        unique=True,
        nullable=False,
    )

    # Channel that produced the lead: website, whatsapp, website_form...
    source: Mapped[str | None] = mapped_column(
        String(50),
        nullable=True,
    )

    # 0-100 qualification score (app.leads.scoring).
    score: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default=text("0"),
        nullable=False,
    )

    # Listing the lead is most interested in (Qdrant property_id).
    property_ref: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
    )

    # Sales-team fields, maintained through the CRM workbook import
    # (app.crm.excel). The AI assistant never writes them.
    assigned_agent: Mapped[str | None] = mapped_column(
        String(80),
        nullable=True,
        index=True,
    )

    last_contacted_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(),
        nullable=True,
    )

    next_followup_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(),
        nullable=True,
        index=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        server_default=func.now(),
        nullable=False,
    )

    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    customer: Mapped["Customer"] = relationship(
        "Customer",
        back_populates="leads",
    )


# ============================================================
# Interaction (conversation turns)
# ============================================================


class Interaction(Base):
    """One customer message and the assistant's reply, per channel."""

    __tablename__ = "interactions"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    public_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        default=uuid4,
        unique=True,
        nullable=False,
    )

    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    lead_id: Mapped[int | None] = mapped_column(
        ForeignKey("leads.id", ondelete="SET NULL"),
        nullable=True,
    )

    # "website:text", "website:voice", "whatsapp:voice_note"...
    channel: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
    )

    direction: Mapped[str] = mapped_column(
        String(10),
        default="inbound",
        nullable=False,
    )

    content: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )

    response: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        server_default=func.now(),
        nullable=False,
        index=True,
    )


# ============================================================
# Human handoff
# ============================================================


class Handoff(Base):
    """A request for a human sales advisor, with the context they need."""

    __tablename__ = "handoffs"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    public_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        default=uuid4,
        unique=True,
        nullable=False,
    )

    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    lead_id: Mapped[int | None] = mapped_column(
        ForeignKey("leads.id", ondelete="SET NULL"),
        nullable=True,
    )

    # open -> claimed -> resolved
    status: Mapped[str] = mapped_column(
        String(20),
        default="open",
        nullable=False,
        index=True,
    )

    reason: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )

    priority: Mapped[str] = mapped_column(
        String(20),
        default="normal",
        nullable=False,
    )

    summary: Mapped[dict[str, Any] | None] = mapped_column(
        JSON,
        nullable=True,
    )

    claimed_by: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
    )

    resolved_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        server_default=func.now(),
        nullable=False,
    )

    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


# ============================================================
# Booking (property viewings)
# ============================================================

BOOKING_ACTIVE_STATUSES = ("pending_confirmation", "confirmed")


class Booking(Base):
    """A property viewing and its lifecycle.

    pending_confirmation -> confirmed -> completed
              \\-> cancelled | expired (an unconfirmed hold that timed out)

    On PostgreSQL an exclusion constraint (see the migration) makes overlapping
    active bookings for one property impossible, whichever worker writes them.
    """

    __tablename__ = "bookings"
    __table_args__ = (
        Index("ix_bookings_property_starts", "property_id", "starts_at"),
        CheckConstraint("ends_at > starts_at", name="ck_bookings_interval"),
    )

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    public_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        default=uuid4,
        unique=True,
        nullable=False,
    )

    # Qdrant/listing property id (a string such as "501189949").
    property_id: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )

    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    lead_id: Mapped[int | None] = mapped_column(
        ForeignKey("leads.id", ondelete="SET NULL"),
        nullable=True,
    )

    # Optional sales advisor the viewing is assigned to.
    agent_id: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
    )

    starts_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        nullable=False,
    )

    ends_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        nullable=False,
    )

    status: Mapped[str] = mapped_column(
        String(30),
        default="pending_confirmation",
        nullable=False,
        index=True,
    )

    # The verified number the advisor will call.
    contact_phone: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
    )

    notes: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    hold_expires_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(),
        nullable=True,
    )

    confirmed_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(),
        nullable=True,
    )

    confirmed_by: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
    )

    cancelled_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(),
        nullable=True,
    )

    # "customer", "staff" or "system".
    cancelled_by: Mapped[str | None] = mapped_column(
        String(20),
        nullable=True,
    )

    cancel_reason: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        server_default=func.now(),
        nullable=False,
    )

    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


# ============================================================
# Staff notification outbox
# ============================================================


class StaffNotification(Base):
    """Alert for the sales team; kept until delivered so none is lost."""

    __tablename__ = "staff_notifications"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    # booking_requested, booking_cancelled, handoff_requested...
    kind: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
    )

    ref_type: Mapped[str | None] = mapped_column(
        String(30),
        nullable=True,
    )

    ref_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )

    message: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )

    payload: Mapped[dict[str, Any] | None] = mapped_column(
        JSON,
        nullable=True,
    )

    # pending -> sent | failed (retried until MAX_ATTEMPTS) | no_channel
    status: Mapped[str] = mapped_column(
        String(20),
        default="pending",
        nullable=False,
        index=True,
    )

    attempts: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
    )

    last_error: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        server_default=func.now(),
        nullable=False,
    )

    sent_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(),
        nullable=True,
    )


# ============================================================
# Replay guard (webhook de-duplication, single-use tokens)
# ============================================================


class ReplayGuard(Base):
    """First-writer-wins record shared by all workers.

    ``kind`` namespaces the key: ``whatsapp`` (provider message ids) and
    ``voice_token`` (token nonces). The primary key is the atomic claim.
    """

    __tablename__ = "replay_guard"

    kind: Mapped[str] = mapped_column(
        String(30),
        primary_key=True,
    )

    key: Mapped[str] = mapped_column(
        String(200),
        primary_key=True,
    )

    # processing -> done | failed
    status: Mapped[str] = mapped_column(
        String(20),
        default="processing",
        nullable=False,
    )

    expires_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        nullable=False,
        index=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        server_default=func.now(),
        nullable=False,
    )


# ============================================================
# Admin audit trail
# ============================================================


class AdminAudit(Base):
    """Who did what to which record through the operator API."""

    __tablename__ = "admin_audit"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    action: Mapped[str] = mapped_column(
        String(60),
        nullable=False,
    )

    target: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
    )

    actor: Mapped[str] = mapped_column(
        String(100),
        default="admin",
        nullable=False,
    )

    client: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )

    detail: Mapped[dict[str, Any] | None] = mapped_column(
        JSON,
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        server_default=func.now(),
        nullable=False,
        index=True,
    )


# ============================================================
# CRM workbook: sales follow-up tasks and duplicate-review decisions
# ============================================================


class CrmTask(Base):
    """A sales follow-up (call, send details, confirm a viewing...).

    ``dedupe_key`` makes system-generated tasks idempotent: the task sync can
    run any number of times, from any worker, and creates each task once.
    """

    __tablename__ = "crm_tasks"
    __table_args__ = (
        Index("ix_crm_tasks_status_due", "status", "due_at"),
        CheckConstraint("status IN ('open', 'in_progress', 'done', 'cancelled')", name="ck_crm_tasks_status"),
        CheckConstraint("priority IN ('low', 'normal', 'high', 'urgent')", name="ck_crm_tasks_priority"),
        CheckConstraint("source IN ('system', 'staff')", name="ck_crm_tasks_source"),
    )

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    public_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        default=uuid4,
        unique=True,
        nullable=False,
    )

    lead_id: Mapped[int] = mapped_column(
        ForeignKey("leads.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    task_type: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
    )

    description: Mapped[str] = mapped_column(
        String(500),
        default="",
        server_default=text("''"),
        nullable=False,
    )

    priority: Mapped[str] = mapped_column(
        String(10),
        default="normal",
        server_default=text("'normal'"),
        nullable=False,
    )

    status: Mapped[str] = mapped_column(
        String(20),
        default="open",
        server_default=text("'open'"),
        nullable=False,
    )

    assigned_agent: Mapped[str | None] = mapped_column(
        String(80),
        nullable=True,
    )

    due_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(),
        nullable=True,
    )

    completed_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(),
        nullable=True,
    )

    # "system" tasks are derived from handoffs/bookings/scores; "staff" tasks
    # were typed into the workbook.
    source: Mapped[str] = mapped_column(
        String(20),
        default="system",
        server_default=text("'system'"),
        nullable=False,
    )

    dedupe_key: Mapped[str | None] = mapped_column(
        String(120),
        unique=True,
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        server_default=func.now(),
        nullable=False,
    )

    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class CrmDuplicateReview(Base):
    """A staff decision about whether two customer records are one person.

    Only the decision is stored; candidate pairs are computed on demand. A
    decision never merges or alters the customer rows - it only controls how
    the workbook groups them for the sales team.
    """

    __tablename__ = "crm_duplicate_reviews"
    __table_args__ = (
        UniqueConstraint("customer_a_id", "customer_b_id", name="uq_crm_duplicate_pair"),
        CheckConstraint("customer_a_id < customer_b_id", name="ck_crm_duplicate_order"),
        CheckConstraint("decision IN ('confirmed', 'rejected')", name="ck_crm_duplicate_decision"),
    )

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    customer_a_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    customer_b_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    decision: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
    )

    # Why the pair was flagged when it was reviewed, e.g. "same_phone".
    evidence: Mapped[str | None] = mapped_column(
        String(60),
        nullable=True,
    )

    reviewed_by: Mapped[str | None] = mapped_column(
        String(80),
        nullable=True,
    )

    reviewed_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        server_default=func.now(),
        nullable=False,
    )
