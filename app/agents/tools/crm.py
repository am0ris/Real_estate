"""CRM operations used by the agents, backed by PostgreSQL.

Agents identify a customer by their conversation key (``web:<session>``,
``whatsapp:<phone>``). Everything written here survives restarts and is shared by
every API worker; staff read it through the admin API.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import select

from app.crm import repository as repo
from app.crm.client import CRMClient
from app.crm.handoffs import DEDUP_WINDOW, HandoffService, serialize as serialize_handoff
from app.database import models as db
from app.helpers.phone import mask_phone


logger = logging.getLogger(__name__)

_LEAD_COLUMNS = {
    "intent", "score", "property_ref", "preferred_location", "preferred_property_type",
    "bedrooms_min", "budget_min", "budget_max", "source",
}

# Automatic status changes only move a lead forward through the pipeline; a
# later chat turn must not demote a lead that already asked for a viewing.
_STATUS_RANK = {
    "new": 0, "qualifying": 1, "qualified": 2, "contact_requested": 3, "hot": 4,
    "viewing_requested": 5, "viewing_booked": 6, "handoff_requested": 7, "contacted": 8,
    # Set by sales staff (CRM workbook); ranked above everything automatic so a
    # later chat turn, booking or handoff can never pull a lead back from it.
    "negotiation": 9,
}


def _jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str, ensure_ascii=False))


class CRMService:

    def __init__(self, client: CRMClient | None = None) -> None:
        self._client = client or CRMClient()
        self._handoffs = HandoffService(self._client)

    # --------------------------------------------------------
    # Customers
    # --------------------------------------------------------

    @staticmethod
    def _profile(customer: db.Customer) -> dict[str, Any]:
        return {
            "id": customer.external_id,
            "ref": str(customer.public_id),
            "name": customer.name,
            "phone": customer.phone,
            "email": customer.email,
            "preferred_language": customer.preferred_language,
            "phone_verified": customer.phone_verified,
        }

    def get_customer(self, customer_id: str) -> dict | None:
        with self._client.session() as session:
            customer = repo.get_customer_row(session, customer_id)
            return self._profile(customer) if customer else None

    def upsert_customer(self, customer_id: str, **fields: object) -> dict:
        """Create the customer if needed and merge in the contact details given."""
        with self._client.session() as session:
            customer = repo.get_or_create_customer(session, customer_id)
            name = str(fields.get("name") or "").strip()
            if name:
                customer.name = name[:255]
            email = str(fields.get("email") or "").strip().lower()
            if email and customer.email != email:
                taken = session.scalar(select(db.Customer.id).where(db.Customer.email == email, db.Customer.id != customer.id))
                if not taken:
                    customer.email = email[:255]
            language = fields.get("language")
            if language in {"ar", "en"}:
                customer.preferred_language = str(language)
            if fields.get("phone"):
                saved = repo.apply_phone(
                    session,
                    customer,
                    str(fields["phone"]),
                    verified=bool(fields.get("phone_verified")),
                    method=str(fields.get("phone_verification_method") or "") or None,
                )
                if saved is None:
                    logger.info("Phone not stored on customer record | %s", mask_phone(str(fields["phone"])))
            session.flush()
            return self._profile(customer)

    # --------------------------------------------------------
    # Leads
    # --------------------------------------------------------

    @staticmethod
    def _lead_dict(lead: db.Lead, customer: db.Customer) -> dict[str, Any]:
        return {
            **(lead.metadata_json or {}),
            "id": str(lead.public_id),
            "customer_id": customer.external_id,
            "status": lead.status,
            "source": lead.source,
            "score": lead.score,
            "property_ref": lead.property_ref,
            "notes": [line for line in (lead.notes or "").split("\n") if line],
        }

    def create_lead(self, customer: dict) -> str:
        """Create a lead for a visitor who has no chat session (website form)."""
        with self._client.session() as session:
            owner = None
            phone = str(customer.get("phone") or "")
            if phone:
                from app.helpers.phone import normalize_phone

                normalized = normalize_phone(phone)
                if normalized:
                    owner = session.scalar(select(db.Customer).where(db.Customer.phone == normalized))
            row = owner or repo.get_or_create_customer(session, f"form:{uuid4().hex}")
            if owner is None:
                if customer.get("name"):
                    row.name = str(customer["name"]).strip()[:255]
                repo.apply_phone(session, row, phone, verified=False)
            lead = repo.ensure_open_lead(session, row, "website_form")
            metadata = dict(lead.metadata_json or {})
            metadata["form_message"] = str(customer.get("message") or "")[:2000]
            lead.metadata_json = metadata
            lead.property_ref = str(customer["property_id"])[:100] if customer.get("property_id") else lead.property_ref
            lead.status = "contact_requested"
            session.flush()
            return str(lead.public_id)

    def ensure_lead(self, customer_id: str, *, source: str) -> str:
        with self._client.session() as session:
            customer = repo.get_or_create_customer(session, customer_id)
            return str(repo.ensure_open_lead(session, customer, source).public_id)

    def get_lead(self, customer_id: str) -> dict | None:
        with self._client.session() as session:
            customer = repo.get_customer_row(session, customer_id)
            if customer is None:
                return None
            lead = repo.latest_lead(session, customer.id)
            return self._lead_dict(lead, customer) if lead else None

    def update_lead(self, customer_id: str, data: dict) -> None:
        with self._client.session() as session:
            customer = repo.get_customer_row(session, customer_id)
            lead = repo.latest_lead(session, customer.id) if customer else None
            if lead is None:
                raise KeyError(f"No lead found for customer {customer_id}")
            metadata = dict(lead.metadata_json or {})
            for key, value in data.items():
                if key == "status":
                    if lead.status in repo.OPEN_LEAD_EXCLUDED:
                        continue  # won/lost/closed are final for automatic updates
                    current = _STATUS_RANK.get(lead.status)
                    incoming = _STATUS_RANK.get(str(value))
                    if current is None or incoming is None or incoming >= current:
                        lead.status = str(value)[:50]
                elif key == "qualification" and isinstance(value, dict):
                    self._apply_qualification(lead, value, metadata)
                elif key in _LEAD_COLUMNS:
                    self._set_column(lead, key, value)
                else:
                    metadata[key] = _jsonable(value)
            lead.metadata_json = metadata

    @staticmethod
    def _set_column(lead: db.Lead, key: str, value: Any) -> None:
        if key in {"budget_min", "budget_max"}:
            setattr(lead, key, repo.to_decimal(value))
        elif key in {"score", "bedrooms_min"}:
            try:
                number = int(value)
            except (TypeError, ValueError):
                return
            setattr(lead, key, max(0, min(100, number)) if key == "score" else number)
        elif value is not None:
            limit = {"preferred_location": 255, "preferred_property_type": 100, "property_ref": 100, "intent": 50, "source": 50}[key]
            setattr(lead, key, str(value)[:limit])

    def _apply_qualification(self, lead: db.Lead, qualification: dict, metadata: dict) -> None:
        mapping = {
            "budget": "budget_max",
            "preferred_location": "preferred_location",
            "property_type": "preferred_property_type",
            "purpose": "intent",
            "score": "score",
        }
        for source_key, column in mapping.items():
            if qualification.get(source_key) not in (None, ""):
                self._set_column(lead, column, qualification[source_key])
        metadata["qualification"] = _jsonable(qualification)

    def add_note(self, customer_id: str, note: str) -> None:
        if not note.strip():
            raise ValueError("note cannot be blank")
        with self._client.session() as session:
            customer = repo.get_customer_row(session, customer_id)
            lead = repo.latest_lead(session, customer.id) if customer else None
            if lead is None:
                raise KeyError(f"No lead found for customer {customer_id}")
            stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
            line = f"[{stamp}] {note.strip()}".replace("\n", " ")
            lead.notes = f"{lead.notes}\n{line}" if lead.notes else line

    # --------------------------------------------------------
    # Conversation -> CRM sync
    # --------------------------------------------------------

    _QUALIFICATION_SIGNALS = ("budget", "purpose", "timeline", "preferred_location", "property_type", "down_payment", "monthly_installment")
    _REQUIREMENT_SIGNALS = ("district", "property_type", "max_price", "bedrooms", "min_bedrooms", "transaction_type")

    def sync_conversation(self, customer_id: str, state: dict[str, Any]) -> None:
        """Keep the CRM in step with what the customer has told us.

        Called after every turn, whichever agent answered and on whichever
        channel, so a customer who shared a budget or area during a plain
        property search is a lead too - not only those who reached the
        qualification, sales or scheduling agents.
        """
        from app.leads.scoring import calculate_lead_score, score_band

        qualification = dict(state.get("qualification") or {})
        requirements = state.get("requirements") or {}
        customer = state.get("customer") or {}
        has_signal = any(qualification.get(key) for key in self._QUALIFICATION_SIGNALS) or any(
            requirements.get(key) for key in self._REQUIREMENT_SIGNALS
        )
        has_contact = any(customer.get(key) for key in ("name", "phone", "email"))
        if not (has_signal or has_contact):
            return

        language = state.get("language")
        self.upsert_customer(customer_id, language=language, **customer)
        if not (has_signal or customer.get("phone")):
            return

        lead_id = self.ensure_lead(customer_id, source=str(state.get("channel") or customer_id.split(":", 1)[0]))
        score = calculate_lead_score(qualification)
        selected = state.get("selected_property") or {}
        update: dict[str, Any] = {
            "qualification": {**qualification, "score": score, "band": score_band(score)},
            "requirements": requirements,
            "status": "qualifying",  # only ever raises a "new" lead; never demotes
        }
        if selected.get("property_id") is not None:
            update["property_ref"] = str(selected["property_id"])
        if requirements.get("min_bedrooms") or requirements.get("bedrooms"):
            update["bedrooms_min"] = requirements.get("min_bedrooms") or requirements.get("bedrooms")
        self.update_lead(customer_id, update)
        logger.debug("CRM synced | lead=%s score=%s", lead_id[:8], score)

    # --------------------------------------------------------
    # Interactions
    # --------------------------------------------------------

    def record_interaction(self, *, customer_id: str, channel: str, content: str, response: str) -> None:
        with self._client.session() as session:
            customer = repo.get_or_create_customer(session, customer_id)
            lead = repo.latest_lead(session, customer.id)
            session.add(
                db.Interaction(
                    customer_id=customer.id,
                    lead_id=lead.id if lead else None,
                    channel=channel[:50],
                    direction="inbound",
                    content=content[:10_000],
                    response=(response or "")[:10_000] or None,
                )
            )

    # --------------------------------------------------------
    # Human handoff
    # --------------------------------------------------------

    def record_handoff(self, customer_id: str, *, source: str, reason: str, priority: str, summary: dict) -> dict:
        """Open a handoff ticket (or return the one opened moments ago) and alert staff."""
        created = False
        with self._client.session() as session:
            customer = repo.get_or_create_customer(session, customer_id)
            lead = repo.ensure_open_lead(session, customer, source)
            handoff = session.scalar(
                select(db.Handoff)
                .where(
                    db.Handoff.customer_id == customer.id,
                    db.Handoff.status.in_(("open", "claimed")),
                    db.Handoff.created_at >= datetime.now(timezone.utc) - DEDUP_WINDOW,
                )
                .order_by(db.Handoff.id.desc())
                .limit(1)
            )
            if handoff is None:
                handoff = db.Handoff(
                    customer_id=customer.id,
                    lead_id=lead.id,
                    status="open",
                    reason=reason[:100],
                    priority=priority[:20],
                    summary=_jsonable(summary),
                )
                session.add(handoff)
                if _STATUS_RANK.get(lead.status, 0) <= _STATUS_RANK["handoff_requested"]:
                    lead.status = "handoff_requested"
                session.flush()
                created = True
            data = serialize_handoff(handoff, customer, lead)
            data["customer_name"] = customer.name
            data["customer_phone"] = (summary.get("contact") or {}).get("phone") or customer.phone
        if created:
            self._alert_handoff(data, source)
        return data

    @staticmethod
    def _alert_handoff(data: dict, source: str) -> None:
        from app.notifications.staff import notify_staff

        summary = data.get("summary") or {}
        requirements = summary.get("requirements") or {}
        interest = ", ".join(f"{key}={value}" for key, value in requirements.items()) or "not specified yet"
        message = (
            f"New handoff request ({data['priority']} priority)\n"
            f"Reason: {data['reason']}\n"
            f"Customer: {data.get('customer_name') or 'unknown'} | phone: {data.get('customer_phone') or 'not provided'}\n"
            f"Channel: {source}\n"
            f"Looking for: {interest}\n"
            f"Property: {summary.get('selected_property_id') or '-'}\n"
            f"Ticket: {data['id'][:8]}"
        )
        notify_staff("handoff_requested", message, ref_type="handoff", ref_id=data["id"])

    def list_handoffs(self, status: str | None = None) -> list[dict]:
        return self._handoffs.list(status)


crm_service = CRMService()
