"""Write a sample CRM workbook (made-up data, no database) to open in Excel.

    python scripts/make_sample_crm_workbook.py [output.xlsx]

Use it to check the layout by eye before connecting the real export endpoint:
the file should open WITHOUT an "we found a problem / repair" prompt, the
dashboard should show numbers, dropdowns should appear on the yellow columns,
and overdue dates should be red.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.crm.excel import signing  # noqa: E402
from app.crm.excel.data import DuplicateRow, ExportData, InteractionRow, LeadRow, TaskRow, ViewingRow  # noqa: E402
from app.crm.excel.exporter import build_workbook  # noqa: E402


def sample_data(now: datetime) -> ExportData:
    def lead(pk: int, **kw) -> LeadRow:
        base = dict(pk=pk, uuid=f"00000000-0000-0000-0000-{pk:012d}", customer_pk=pk, name=f"Customer {pk}", phone=f"+2010000000{pk:02d}",
                    channel="website", created_at=now - timedelta(days=pk * 3), score=50, band="warm", status="new", language="en")
        base.update(kw)
        return LeadRow(**base)

    leads = [
        lead(1, name="أحمد محمد", language="ar", status="qualifying", location="New Cairo", transaction_type="sale", property_type="villa",
             budget_max=Decimal(9_000_000), bedrooms=4, qualified=True, band="hot", score=88, phone_verified=True, next_task="Call back today",
             requirements="finishing: full; amenities: pool, gym", last_message_at=now - timedelta(hours=3)),
        lead(2, name="Mona Samir", channel="whatsapp", status="qualified", location="New Cairo", transaction_type="rent", qualified=True,
             assigned_agent="Mona", notes="[2026-11-01 10:00] asked for a viewing", next_viewing_at=now + timedelta(days=1)),
        lead(3, name="Omar Khaled", channel="whatsapp", status="contacted", location="Sheikh Zayed", band="cold", assigned_agent="Ahmed",
             next_followup_at=now - timedelta(days=2), last_contact_at=now - timedelta(days=5)),
        lead(4, name="=HYPERLINK(\"http://example.com\",\"click\")", channel="website_form", status="won", location="Sheikh Zayed", transaction_type="sale", qualified=True),
        lead(5, name="Layla Hassan", status="lost", location="6th October"),
        lead(6, name="Youssef Adel", status="negotiation", location="6th October", assigned_agent="Mona", next_followup_at=now,
             open_handoff=True, possible_duplicate=True, person_key="P-000006"),
    ]
    tasks = [
        TaskRow(pk=1, uuid="t-1", lead_pk=1, customer="أحمد محمد", phone="+201000000001", task_type="call_customer", description="Customer asked to speak with sales",
                assigned_agent="Mona", due_at=now, priority="high", status="open", source="system", created_at=now),
        TaskRow(pk=2, uuid="t-2", lead_pk=3, customer="Omar Khaled", task_type="confirm_viewing", description="Confirm Saturday viewing",
                due_at=now - timedelta(days=3), priority="urgent", status="in_progress", source="system", created_at=now),
        TaskRow(pk=3, uuid="t-3", lead_pk=2, customer="Mona Samir", task_type="send_details", description="Send floor plans",
                due_at=now + timedelta(days=4), priority="low", status="open", source="staff", created_at=now),
        TaskRow(pk=4, uuid="t-4", lead_pk=2, customer="Mona Samir", task_type="other", description="Done example", status="done",
                completed_at=now, due_at=now - timedelta(days=1), source="staff", created_at=now),
    ]
    viewings = [
        ViewingRow(1, 2, "Mona Samir", "+201000000002", "501189810", now + timedelta(days=1), now + timedelta(days=1, hours=1), "confirmed", "Ahmed"),
        ViewingRow(2, 3, "Omar Khaled", "", "501189811", now + timedelta(days=2), now + timedelta(days=2, hours=1), "pending_confirmation"),
    ]
    interactions = [
        InteractionRow(1, 1, "أحمد محمد", now - timedelta(hours=3), "website", "text", "عايز فيلا في التجمع الخامس ميزانيتي 9 مليون", "ممكن أرشحلك فيلات في التجمع الخامس..."),
        InteractionRow(2, 2, "Mona Samir", now - timedelta(days=1), "whatsapp", "voice", "=1+1 (this must stay text)", "Sure, here are the options."),
    ]
    duplicates = [
        DuplicateRow(1, 2, "أحمد محمد", "+201000000001", "website", False, "Ahmed M.", "+201000000001", "whatsapp", True, "same_phone"),
    ]
    return ExportData(generated_at=now, leads=leads, tasks=tasks, viewings=viewings, interactions=interactions, duplicates=duplicates)


def main() -> None:
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "sample_crm_workbook.xlsx")
    now = datetime.now(timezone.utc).replace(microsecond=0)
    blob = build_workbook(
        sample_data(now),
        agents=["Mona", "Ahmed"],
        timezone="Africa/Cairo",
        signing_key=signing.derive_key(None, "sample-only-key-not-used-anywhere-0123"),
    )
    target.write_bytes(blob)
    print(f"Wrote {target} ({len(blob) / 1024:.0f} KB). Open it in Excel.")


if __name__ == "__main__":
    main()
