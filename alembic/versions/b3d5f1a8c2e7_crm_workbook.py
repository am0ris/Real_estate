"""crm workbook: sales fields, follow-up tasks, duplicate reviews

Backs the Excel CRM workbook (app/crm/excel). Everything is additive and
nullable/defaulted, so existing rows and the running application are
unaffected:

* leads: assigned_agent, last_contacted_at, next_followup_at (sales-team fields)
* crm_tasks: follow-up tasks, idempotent through a unique dedupe_key
* crm_duplicate_reviews: staff decisions about possible duplicate customers

The "negotiation" lead stage needs no schema change (leads.status is free text).

Revision ID: b3d5f1a8c2e7
Revises: a7c1e0d2b9f4
Create Date: 2026-11-06 12:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "b3d5f1a8c2e7"
down_revision: Union[str, None] = "a7c1e0d2b9f4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _ts(name: str, *, nullable: bool = False, default_now: bool = False) -> sa.Column:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        server_default=sa.text("now()") if default_now else None,
        nullable=nullable,
    )


def upgrade() -> None:
    op.add_column("leads", sa.Column("assigned_agent", sa.String(length=80), nullable=True))
    op.add_column("leads", _ts("last_contacted_at", nullable=True))
    op.add_column("leads", _ts("next_followup_at", nullable=True))
    op.create_index(op.f("ix_leads_assigned_agent"), "leads", ["assigned_agent"], unique=False)
    op.create_index(op.f("ix_leads_next_followup_at"), "leads", ["next_followup_at"], unique=False)

    op.create_table(
        "crm_tasks",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("public_id", sa.Uuid(), nullable=False),
        sa.Column("lead_id", sa.Integer(), nullable=False),
        sa.Column("task_type", sa.String(length=30), nullable=False),
        sa.Column("description", sa.String(length=500), server_default=sa.text("''"), nullable=False),
        sa.Column("priority", sa.String(length=10), server_default=sa.text("'normal'"), nullable=False),
        sa.Column("status", sa.String(length=20), server_default=sa.text("'open'"), nullable=False),
        sa.Column("assigned_agent", sa.String(length=80), nullable=True),
        _ts("due_at", nullable=True),
        _ts("completed_at", nullable=True),
        sa.Column("source", sa.String(length=20), server_default=sa.text("'system'"), nullable=False),
        sa.Column("dedupe_key", sa.String(length=120), nullable=True),
        _ts("created_at", default_now=True),
        _ts("updated_at", default_now=True),
        sa.CheckConstraint("status IN ('open', 'in_progress', 'done', 'cancelled')", name="ck_crm_tasks_status"),
        sa.CheckConstraint("priority IN ('low', 'normal', 'high', 'urgent')", name="ck_crm_tasks_priority"),
        sa.CheckConstraint("source IN ('system', 'staff')", name="ck_crm_tasks_source"),
        sa.ForeignKeyConstraint(["lead_id"], ["leads.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("public_id"),
        sa.UniqueConstraint("dedupe_key"),
    )
    op.create_index(op.f("ix_crm_tasks_lead_id"), "crm_tasks", ["lead_id"], unique=False)
    op.create_index("ix_crm_tasks_status_due", "crm_tasks", ["status", "due_at"], unique=False)

    op.create_table(
        "crm_duplicate_reviews",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("customer_a_id", sa.Integer(), nullable=False),
        sa.Column("customer_b_id", sa.Integer(), nullable=False),
        sa.Column("decision", sa.String(length=20), nullable=False),
        sa.Column("evidence", sa.String(length=60), nullable=True),
        sa.Column("reviewed_by", sa.String(length=80), nullable=True),
        _ts("reviewed_at", default_now=True),
        sa.CheckConstraint("customer_a_id < customer_b_id", name="ck_crm_duplicate_order"),
        sa.CheckConstraint("decision IN ('confirmed', 'rejected')", name="ck_crm_duplicate_decision"),
        sa.ForeignKeyConstraint(["customer_a_id"], ["customers.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["customer_b_id"], ["customers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("customer_a_id", "customer_b_id", name="uq_crm_duplicate_pair"),
    )
    op.create_index(op.f("ix_crm_duplicate_reviews_customer_a_id"), "crm_duplicate_reviews", ["customer_a_id"], unique=False)
    op.create_index(op.f("ix_crm_duplicate_reviews_customer_b_id"), "crm_duplicate_reviews", ["customer_b_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_crm_duplicate_reviews_customer_b_id"), table_name="crm_duplicate_reviews")
    op.drop_index(op.f("ix_crm_duplicate_reviews_customer_a_id"), table_name="crm_duplicate_reviews")
    op.drop_table("crm_duplicate_reviews")

    op.drop_index("ix_crm_tasks_status_due", table_name="crm_tasks")
    op.drop_index(op.f("ix_crm_tasks_lead_id"), table_name="crm_tasks")
    op.drop_table("crm_tasks")

    op.drop_index(op.f("ix_leads_next_followup_at"), table_name="leads")
    op.drop_index(op.f("ix_leads_assigned_agent"), table_name="leads")
    op.drop_column("leads", "next_followup_at")
    op.drop_column("leads", "last_contacted_at")
    op.drop_column("leads", "assigned_agent")
