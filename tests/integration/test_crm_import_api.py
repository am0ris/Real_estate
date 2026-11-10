"""POST /admin/crm/import over HTTP: who may call it, what it accepts, and what it reports."""

from __future__ import annotations

import io

import pytest
from openpyxl import load_workbook

from app.crm.excel import schema as sc
from app.crm.excel.importer import crm_import_service
from app.database import models as db
from app.database.session import session_scope
from tests.integration.test_api_channels import client  # noqa: F401  (fixture)
from tests.integration.test_crm_excel_queries import make_lead
from tests.integration.test_crm_import import CONTACTED, Editor, lead_of, ref

IMPORT = "/admin/crm/import"
EXPORT = "/admin/crm/export.xlsx"
ADMIN = {"X-Admin-Key": "test-admin-key-0123456789abcdef"}
CRM_KEY = "sales-team-key-0123456789abcdefghij"
RAW = {**ADMIN, "Content-Type": "application/octet-stream"}


@pytest.fixture(autouse=True)
def roster(settings_env):
    settings_env(CRM_AGENTS="Mona, Ahmed")


def exported(client):
    response = client.get(EXPORT, headers=ADMIN)
    assert response.status_code == 200
    return response.content


def audit_actions():
    with session_scope() as session:
        return [a.action for a in session.query(db.AdminAudit).order_by(db.AdminAudit.id)]


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------


def test_the_import_rejects_missing_wrong_and_near_miss_keys(client):
    blob = exported(client)
    assert client.post(IMPORT, content=blob).status_code == 401
    assert client.post(IMPORT, content=blob, headers={"X-Admin-Key": "wrong"}).status_code == 401
    assert client.post(IMPORT, content=blob, headers={"X-Admin-Key": ADMIN["X-Admin-Key"] + "x"}).status_code == 401
    assert client.post(IMPORT, content=blob, headers=RAW).status_code == 200


def test_an_unauthorised_caller_cannot_change_anything_even_with_a_valid_file(client):
    _, lead = make_lead()
    blob = Editor(exported(client)).lead(lead, status=CONTACTED).bytes()
    assert client.post(IMPORT, params={"dry_run": "false"}, content=blob, headers={"X-Admin-Key": "wrong"}).status_code == 401
    assert lead_of(lead)["status"] == "new" and "crm.import" not in audit_actions()


def test_the_key_check_happens_before_the_body_is_read(client):
    """A huge body from someone without a key is refused with 401, not buffered and parsed."""
    response = client.post(IMPORT, content=b"x" * 3_000_000, headers={"X-Admin-Key": "wrong", "Content-Type": "application/octet-stream"})
    assert response.status_code == 401


def test_the_sales_key_can_import_and_other_admin_routes_stay_closed_to_it(client, settings_env):
    settings_env(CRM_API_KEY=CRM_KEY)
    blob = exported(client)
    assert client.post(IMPORT, content=blob, headers={"X-Admin-Key": CRM_KEY, "Content-Type": "application/octet-stream"}).status_code == 200
    assert client.get("/admin/bookings", headers={"X-Admin-Key": CRM_KEY}).status_code == 401


# ---------------------------------------------------------------------------
# What it accepts
# ---------------------------------------------------------------------------


def test_files_that_are_not_valid_workbooks_get_a_clear_400(client):
    for body, message in ((b"", "empty"), (b"just text", "not an .xlsx"), (b"PK\x03\x04junk", "damaged")):
        response = client.post(IMPORT, content=body, headers=RAW)
        assert response.status_code == 400 and message in response.json()["detail"], (body, response.text)


def test_a_tampered_workbook_is_refused(client):
    book = load_workbook(io.BytesIO(exported(client)))
    book["_meta"].cell(row=8, column=3).value = '{"status":"won"}'
    out = io.BytesIO(); book.save(out)
    response = client.post(IMPORT, content=out.getvalue(), headers=RAW)
    assert response.status_code == 400 and "signature" in response.json()["detail"]


def test_a_multipart_form_is_refused_with_instructions(client):
    response = client.post(IMPORT, files={"file": ("crm.xlsx", exported(client))}, headers=ADMIN)
    assert response.status_code == 415 and "--data-binary" in response.json()["detail"]


def test_an_oversized_upload_is_refused_without_being_processed(client, settings_env):
    settings_env(CRM_IMPORT_MAX_MB="1")
    response = client.post(IMPORT, content=b"x" * (1024 * 1024 + 10), headers=RAW)
    assert response.status_code == 413


def test_query_flags_must_be_booleans(client):
    assert client.post(IMPORT, params={"dry_run": "maybe"}, content=exported(client), headers=RAW).status_code == 422


# ---------------------------------------------------------------------------
# What it does
# ---------------------------------------------------------------------------


def test_a_preview_reports_the_plan_and_changes_nothing(client):
    _, lead = make_lead()
    blob = Editor(exported(client)).lead(lead, status=CONTACTED, assigned_agent="Mona").bytes()
    response = client.post(IMPORT, content=blob, headers=RAW)
    body = response.json()
    assert response.status_code == 200 and body["dry_run"] is True and body["applied"] is False
    assert body["summary"]["fields_changed"] == 2 and body["rows"][0]["ref"] == ref(lead) and body["rows"][0]["result"] == "would_apply" and body["summary"]["would_apply"] == 1
    assert "no-store" in response.headers["cache-control"]
    assert lead_of(lead)["status"] == "new" and audit_actions()[-1] == "crm.import.preview"


def test_the_full_round_trip_export_edit_import_export(client):
    _, lead = make_lead(name="Sara")
    blob = Editor(exported(client)).lead(lead, status=CONTACTED, assigned_agent="ahmed", notes="Spoke to her").bytes()

    applied = client.post(IMPORT, params={"dry_run": "false"}, content=blob, headers=RAW).json()
    assert applied["applied"] is True and applied["summary"]["errors"] == 0 and audit_actions()[-1] == "crm.import"

    again = load_workbook(io.BytesIO(exported(client)))[sc.LEADS]
    cell = lambda key: again.cell(row=2, column=sc.column_index(sc.LEADS, key)).value  # noqa: E731
    assert (cell("status"), cell("assigned_agent"), cell("notes")) == (sc.LEAD_STATUS["contacted"], "Ahmed", "Spoke to her")


def test_problems_come_back_as_a_readable_report_not_an_error_status(client):
    _, lead = make_lead()
    blob = Editor(exported(client)).lead(lead, status=sc.LEAD_STATUS["hot"], assigned_agent="Stranger").bytes()
    response = client.post(IMPORT, params={"dry_run": "false"}, content=blob, headers=RAW)
    row = response.json()["rows"][0]
    assert response.status_code == 200 and row["result"] == "error" and len(row["errors"]) == 2
    assert lead_of(lead)["status"] == "new"


def test_a_second_import_while_one_is_running_is_told_to_retry(client, monkeypatch):
    monkeypatch.setattr(crm_import_service, "_wait", 0.05)
    blob = exported(client)
    assert crm_import_service._gate.acquire(timeout=1)
    try:
        response = client.post(IMPORT, content=blob, headers=RAW)
    finally:
        crm_import_service._gate.release()
    assert response.status_code == 429 and response.headers["retry-after"] == "15"
    assert client.post(IMPORT, content=blob, headers=RAW).status_code == 200


def test_the_audit_log_records_the_import_without_note_text(client):
    import json

    _, lead = make_lead()
    blob = Editor(exported(client)).lead(lead, status=CONTACTED, notes="private customer details 0100 123 4567").bytes()
    client.post(IMPORT, params={"dry_run": "false"}, content=blob, headers=RAW)
    with session_scope() as session:
        entry = session.query(db.AdminAudit).filter_by(action="crm.import").one()
        text = json.dumps(entry.detail)
    assert "0100" not in text and entry.detail["was_applied"] is True and entry.detail["fields_changed"] >= 2
