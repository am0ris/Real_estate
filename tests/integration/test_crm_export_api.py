"""GET /admin/crm/export.xlsx: access control, the file it returns, and that it can never hurt a conversation."""

from __future__ import annotations

import io
import json

import pytest
from openpyxl import load_workbook

from app.crm.excel import schema as sc, signing
from app.crm.excel.meta import read_meta, verify_meta
from app.crm.excel.service import crm_workbook_service
from app.database import models as db
from app.database.session import session_scope
from tests.integration.test_admin_api import pending  # noqa: F401  (fixture: one viewing waiting for confirmation)
from tests.integration.test_api_channels import client  # noqa: F401  (fixture)
from tests.integration.test_crm_excel_queries import make_lead

PATH = "/admin/crm/export.xlsx"
ADMIN = {"X-Admin-Key": "test-admin-key-0123456789abcdef"}
CRM_KEY = "sales-team-key-0123456789abcdefghij"
CRM = {"X-Admin-Key": CRM_KEY}


def workbook(response):
    assert response.status_code == 200, response.text
    return load_workbook(io.BytesIO(response.content))


def counts():
    with session_scope() as session:
        return session.query(db.CrmTask).count(), session.query(db.AdminAudit).count()


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------


def test_the_export_rejects_missing_wrong_and_near_miss_keys(client):
    assert client.get(PATH).status_code == 401
    assert client.get(PATH, headers={"X-Admin-Key": "wrong"}).status_code == 401
    assert client.get(PATH, headers={"X-Admin-Key": ADMIN["X-Admin-Key"] + " "}).status_code == 401
    assert client.get(PATH, headers={"X-Admin-Key": ADMIN["X-Admin-Key"][:-1]}).status_code == 401
    assert client.get(PATH, headers=ADMIN).status_code == 200


def test_a_rejected_request_does_no_work_and_leaves_no_audit_trail(client, pending):  # noqa: F811
    before = counts()
    assert client.get(PATH, headers={"X-Admin-Key": "wrong"}).status_code == 401
    assert counts() == before  # no tasks created, nothing audited


def test_the_export_is_disabled_when_no_key_is_configured(client, settings_env):
    settings_env(ADMIN_API_KEY="", CRM_API_KEY="")
    assert client.get(PATH, headers=ADMIN).status_code == 503


def test_the_sales_key_opens_the_export_and_the_admin_key_still_does(client, settings_env):
    settings_env(CRM_API_KEY=CRM_KEY)
    assert client.get(PATH, headers=CRM).status_code == 200
    assert client.get(PATH, headers=ADMIN).status_code == 200
    assert client.get(PATH, headers={"X-Admin-Key": "wrong"}).status_code == 401


def test_the_sales_key_does_not_open_any_other_admin_route(client, settings_env):
    settings_env(CRM_API_KEY=CRM_KEY)
    assert client.get("/admin/bookings", headers=CRM).status_code == 401
    assert client.get("/admin/leads", headers=CRM).status_code == 401


def test_the_key_is_only_read_from_the_header(client):
    assert client.get(PATH, params={"X-Admin-Key": ADMIN["X-Admin-Key"], "admin_key": ADMIN["X-Admin-Key"]}).status_code == 401


def test_repeated_wrong_keys_lock_the_client_out_of_the_export(client, settings_env):
    settings_env(RATE_LIMIT_ENABLED="true", RATE_LIMIT_ADMIN_FAILURES_PER_15MIN="4")
    codes = [client.get(PATH, headers={"X-Admin-Key": f"guess-{i}"}).status_code for i in range(6)]
    assert codes[:4] == [401] * 4 and set(codes[4:]) == {429}
    assert client.get(PATH, headers=ADMIN).status_code == 429  # even the right key waits out the lock-out


# ---------------------------------------------------------------------------
# The file
# ---------------------------------------------------------------------------


def test_the_response_is_a_private_downloadable_xlsx(client):
    response = client.get(PATH, headers=ADMIN)
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    assert response.headers["content-disposition"].startswith('attachment; filename="crm-') and response.headers["content-disposition"].endswith('.xlsx"')
    assert "no-store" in response.headers["cache-control"] and response.headers["x-content-type-options"] == "nosniff"
    assert response.content[:2] == b"PK"


def test_the_file_is_signed_with_a_key_derived_from_the_server_secret(client):
    meta = read_meta(workbook(client.get(PATH, headers=ADMIN)))
    verify_meta(meta, signing.derive_key(None, ADMIN["X-Admin-Key"]))
    with pytest.raises(Exception):
        verify_meta(meta, signing.derive_key(None, "some-other-deployments-key-0123456789"))


def test_an_explicit_signing_key_is_used_when_configured(client, settings_env):
    settings_env(CRM_SIGNING_KEY="a-dedicated-signing-key-0123456789")
    meta = read_meta(workbook(client.get(PATH, headers=ADMIN)))
    verify_meta(meta, signing.derive_key("a-dedicated-signing-key-0123456789", None))


def test_the_workbook_shows_the_agents_from_settings(client, settings_env):
    settings_env(CRM_AGENTS="Mona, Ahmed")
    lists = workbook(client.get(PATH, headers=ADMIN))["Lists"]
    assert [c.value for c in lists["B"][1:] if c.value] == ["Mona", "Ahmed"]


def test_the_workbook_contains_the_real_leads_and_the_tasks_they_call_for(client, pending):  # noqa: F811
    book = workbook(client.get(PATH, headers=ADMIN))
    leads, tasks, viewings = book["Leads"], book["Tasks"], book["Viewings"]
    assert leads.cell(row=2, column=sc.column_index("Leads", "channel")).value == "whatsapp"
    assert tasks.cell(row=2, column=sc.column_index("Tasks", "task_type")).value == "Confirm a viewing | تأكيد معاينة"
    assert tasks.cell(row=2, column=sc.column_index("Tasks", "status")).value == "Open | مفتوحة"
    assert viewings.cell(row=2, column=sc.column_index("Viewings", "status")).value == "Pending confirmation | في انتظار التأكيد"


def test_exporting_again_does_not_duplicate_tasks(client, pending):  # noqa: F811
    first = workbook(client.get(PATH, headers=ADMIN))["Tasks"].max_row
    second = workbook(client.get(PATH, headers=ADMIN))["Tasks"].max_row
    assert first == second
    with session_scope() as session:
        assert session.query(db.CrmTask).count() == 1


def test_a_customers_phone_number_never_reaches_the_audit_log(client, pending):  # noqa: F811
    assert client.get(PATH, headers=ADMIN).status_code == 200
    with session_scope() as session:
        entry = session.query(db.AdminAudit).filter_by(action="crm.export").one()
        text = json.dumps(entry.detail)
    assert entry.target is None and "201012345678" not in text
    assert {"leads", "tasks", "interactions", "tasks_created", "bytes"} <= set(entry.detail)


def test_query_parameters_are_validated_and_closed_leads_can_be_left_out(client):
    make_lead(status="qualified", name="Open One")
    make_lead(status="won", name="Closed One")
    names = lambda book: [c.value for c in book["Leads"]["C"][1:] if c.value]  # noqa: E731
    assert sorted(names(workbook(client.get(PATH, headers=ADMIN)))) == ["Closed One", "Open One"]
    assert names(workbook(client.get(PATH, params={"include_closed": "false"}, headers=ADMIN))) == ["Open One"]
    assert client.get(PATH, params={"interaction_days": 0}, headers=ADMIN).status_code == 422
    assert client.get(PATH, params={"interaction_days": 400}, headers=ADMIN).status_code == 422


# ---------------------------------------------------------------------------
# Failure and overload behaviour
# ---------------------------------------------------------------------------


def test_a_failure_while_creating_tasks_does_not_fail_the_export(client, monkeypatch, pending):  # noqa: F811
    import app.crm.excel.service as service

    def boom(*args, **kwargs):
        raise RuntimeError("task sync exploded")

    monkeypatch.setattr(service, "sync_system_tasks", boom)
    book = workbook(client.get(PATH, headers=ADMIN))
    assert book["Leads"].max_row >= 2  # the data is still there


def test_a_second_export_while_one_is_running_is_told_to_retry(client, monkeypatch):
    monkeypatch.setattr(crm_workbook_service, "_wait", 0.05)
    assert crm_workbook_service._gate.acquire(timeout=1)
    try:
        response = client.get(PATH, headers=ADMIN)
    finally:
        crm_workbook_service._gate.release()
    assert response.status_code == 429 and response.headers["retry-after"] == "15"
    assert client.get(PATH, headers=ADMIN).status_code == 200  # and it recovers


def test_the_slot_is_released_when_building_fails(client, monkeypatch):
    import app.crm.excel.service as service

    monkeypatch.setattr(service, "build_workbook", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk full")))
    with pytest.raises(RuntimeError):
        client.get(PATH, headers=ADMIN)
    monkeypatch.undo()
    assert client.get(PATH, headers=ADMIN).status_code == 200  # the failed request did not leave the gate locked


def test_the_assistant_keeps_working_while_the_crm_export_is_broken(client, monkeypatch, stub_rag):
    import app.crm.excel.service as service

    monkeypatch.setattr(service, "load_export_data", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    with pytest.raises(RuntimeError):
        client.get(PATH, headers=ADMIN)
    reply = client.post("/chat", json={"query": "apartments in New Cairo", "session_id": None})
    assert reply.status_code == 200
