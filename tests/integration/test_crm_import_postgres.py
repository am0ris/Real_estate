"""Two imports editing the same lead at the same moment (PostgreSQL only: skipped unless TEST_DATABASE_URL is set)."""

from __future__ import annotations

import threading

import pytest

from app.crm.excel.importer import CrmImportService
from app.crm.excel.service import crm_workbook_service
from tests.integration.test_crm_excel_queries import make_lead
from tests.integration.test_crm_import import Editor, lead_of
from tests.integration.test_postgres import pg  # noqa: F401  (fixture: a freshly migrated PostgreSQL)

pytestmark = pytest.mark.postgres


def test_two_people_editing_the_same_field_from_one_export_never_lose_an_update(pg, settings_env):  # noqa: F811
    settings_env(CRM_AGENTS="Mona, Ahmed")
    _, lead = make_lead()
    blob = crm_workbook_service.export().content
    by_mona = Editor(blob).lead(lead, assigned_agent="Mona").bytes()
    by_ahmed = Editor(blob).lead(lead, assigned_agent="Ahmed").bytes()

    barrier, results, errors = threading.Barrier(2), {}, []

    def run(name, file):
        try:
            barrier.wait(timeout=10)
            # separate service objects: no in-process gate, so the database row lock is what is being tested
            results[name] = CrmImportService().run(file, dry_run=False)
        except Exception as error:  # pragma: no cover
            errors.append(error)

    threads = [threading.Thread(target=run, args=(name, file)) for name, file in (('Mona', by_mona), ('Ahmed', by_ahmed))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert not errors, errors
    summaries = {name: result.report["summary"] for name, result in results.items()}
    assert sum(s["applied"] for s in summaries.values()) == 1 and sum(s["conflicts"] for s in summaries.values()) == 1  # one won, the other saw a conflict
    winner = next(name for name, s in summaries.items() if s["applied"] == 1)
    assert lead_of(lead)["assigned_agent"] == winner  # and the database holds exactly the winner's value
