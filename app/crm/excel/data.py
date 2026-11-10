"""Plain data handed from the database layer to the exporter (pure).

Keeping the exporter free of ORM objects means it can be tested without a
database and cannot trigger lazy queries while a workbook is being written.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True)
class LeadRow:
    pk: int
    uuid: str
    customer_pk: int
    name: str = ""
    phone: str = ""
    phone_verified: bool = False
    email: str = ""
    channel: str = ""
    created_at: datetime | None = None
    language: str = ""
    transaction_type: str | None = None  # "sale" | "rent" | None
    location: str = ""
    compound: str = ""
    property_type: str = ""
    budget_min: Decimal | None = None
    budget_max: Decimal | None = None
    bedrooms: int | None = None
    requirements: str = ""
    property_ref: str = ""
    score: int = 0
    band: str = "cold"
    qualified: bool = False
    status: str = "new"
    assigned_agent: str = ""
    next_followup_at: datetime | None = None
    last_contact_at: datetime | None = None  # staff-entered, else derived from handoffs
    stored_last_contact_at: datetime | None = None  # exactly what the column holds
    notes: str = ""
    last_message_at: datetime | None = None
    next_viewing_at: datetime | None = None
    open_handoff: bool = False
    next_task: str = ""
    person_key: str = ""
    possible_duplicate: bool = False


@dataclass(frozen=True)
class TaskRow:
    pk: int
    uuid: str
    lead_pk: int
    customer: str = ""
    phone: str = ""
    task_type: str = "other"
    description: str = ""
    assigned_agent: str = ""
    due_at: datetime | None = None
    priority: str = "normal"
    status: str = "open"
    completed_at: datetime | None = None
    source: str = "system"
    created_at: datetime | None = None


@dataclass(frozen=True)
class InteractionRow:
    pk: int
    lead_pk: int | None
    customer: str
    created_at: datetime | None
    channel: str
    modality: str
    customer_message: str
    assistant_response: str


@dataclass(frozen=True)
class ViewingRow:
    pk: int
    lead_pk: int | None
    customer: str
    phone: str
    property_id: str
    starts_at: datetime | None
    ends_at: datetime | None
    status: str
    agent: str = ""
    notes: str = ""


@dataclass(frozen=True)
class DuplicateRow:
    customer_a_pk: int
    customer_b_pk: int
    a_name: str
    a_phone: str
    a_channel: str
    a_verified: bool
    b_name: str
    b_phone: str
    b_channel: str
    b_verified: bool
    evidence: str
    decision: str = ""  # "" pending | "confirmed" | "rejected"
    reviewed_by: str = ""
    reviewed_at: datetime | None = None

    @property
    def pair_key(self) -> str:
        return f"{self.customer_a_pk}:{self.customer_b_pk}"


@dataclass
class ExportData:
    generated_at: datetime
    leads: list[LeadRow] = field(default_factory=list)
    tasks: list[TaskRow] = field(default_factory=list)
    interactions: list[InteractionRow] = field(default_factory=list)
    viewings: list[ViewingRow] = field(default_factory=list)
    duplicates: list[DuplicateRow] = field(default_factory=list)
    # How many rows exist beyond the export caps, per sheet (empty = nothing cut).
    truncated: dict[str, int] = field(default_factory=dict)
    include_closed: bool = True
    interaction_days: int = 90
