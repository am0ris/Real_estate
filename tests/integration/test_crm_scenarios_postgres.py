"""Scenarios 6 and 7 with real threads. PostgreSQL only: skipped unless TEST_DATABASE_URL is set
(see tests/integration/test_postgres.py for the one-line docker command)."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta
from uuid import uuid4

import pytest

from app.crm.excel import schema as sc
from app.crm.excel.importer import CrmImportService
from app.crm.excel.service import crm_workbook_service
from tests.integration.test_booking_flows import say
from tests.integration.test_crm_import import Editor
from tests.integration.test_crm_scenarios import TODAY, rows
from tests.integration.test_postgres import pg  # noqa: F401  (fixture: a freshly migrated PostgreSQL)

pytestmark = pytest.mark.postgres


def run_together(jobs):
    """Run callables at the same instant in separate threads; return their errors."""
    barrier, errors = threading.Barrier(len(jobs)), []

    def wrap(job):
        try:
            barrier.wait(timeout=15)
            job()
        except Exception as error:  # pragma: no cover - reported by the assertions
            errors.append(error)

    threads = [threading.Thread(target=wrap, args=(job,)) for job in jobs]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)
    return errors


def test_6_twelve_customers_chatting_while_the_team_downloads_the_workbook(pg, service, stub_rag):  # noqa: F811
    districts = ["New Cairo", "Sheikh Zayed"]
    threads = [f"web:{uuid4().hex}" for _ in range(12)]
    exports = []

    def customer(index):
        def job():
            say(service, threads[index], f"apartment in {districts[index % 2]}")
            say(service, threads[index], f"my budget is {index + 2} million")
        return job

    errors = run_together([customer(i) for i in range(12)] + [lambda: exports.append(crm_workbook_service.export().content)] * 2)
    assert not errors, errors
    assert len(exports) == 2  # downloading mid-conversation worked, twice at once

    leads = rows(crm_workbook_service.export().content, sc.LEADS)
    assert len(leads) == 12 and len({l["lead_uuid"] for l in leads}) == 12
    assert {l["budget_max"]: l["location"] for l in leads} == {(i + 2) * 1_000_000: districts[i % 2] for i in range(12)}  # nobody's details got mixed up
    assert len(rows(crm_workbook_service.export().content, sc.INTERACTIONS)) == 24


def test_7_a_salesperson_edits_while_the_customer_keeps_chatting(pg, service, stub_rag, settings_env):  # noqa: F811
    settings_env(CRM_AGENTS="Mona, Ahmed")
    thread = f"web:{uuid4().hex}"
    say(service, thread, "apartment in New Cairo")
    file = crm_workbook_service.export().content
    (lead_row,) = rows(file, sc.LEADS)
    pk = int(lead_row["lead_ref"][2:])
    follow_up = datetime(TODAY.year, TODAY.month, TODAY.day) + timedelta(days=2)
    results = []

    def staff():
        results.append(CrmImportService().run(Editor(file).lead(pk, assigned_agent="Mona", next_followup=follow_up).bytes(), dry_run=False))

    def customer():
        for text in ("my budget is 5 million", "for investment, within 3 months", "villa in Sheikh Zayed"):
            say(service, thread, text)

    errors = run_together([staff, customer])
    assert not errors, errors

    (after,) = rows(crm_workbook_service.export().content, sc.LEADS)
    assert results[0].applied is True and results[0].report["summary"]["errors"] == 0
    assert after["assigned_agent"] == "Mona" and after["next_followup"].date() == TODAY + timedelta(days=2)  # the salesperson's edit survived
    assert after["budget_max"] == 5_000_000 and after["qualified"] == sc.YES  # and so did everything the assistant learned meanwhile
