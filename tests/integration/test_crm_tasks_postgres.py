"""The task sync under real concurrency. PostgreSQL only: skipped unless TEST_DATABASE_URL is set
(see tests/integration/test_postgres.py for the one-line docker command)."""

from __future__ import annotations

import threading

import pytest

from app.database import models as db
from app.database.session import session_scope
from tests.integration.test_crm_excel_queries import make_lead
from tests.integration.test_crm_tasks_sync import sync
from tests.integration.test_postgres import pg  # noqa: F401  (fixture: a freshly migrated PostgreSQL)

pytestmark = pytest.mark.postgres


def test_many_syncs_at_once_create_every_task_exactly_once(pg):  # noqa: F811
    for _ in range(40):
        make_lead(score=90)

    workers, barrier = 8, threading.Barrier(8)
    created, errors = [], []

    def run():
        try:
            barrier.wait(timeout=10)  # start together so the inserts really collide
            created.append(sync(inactive_days=0).created)
        except Exception as error:  # pragma: no cover - reported by the assertion below
            errors.append(error)

    threads = [threading.Thread(target=run) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert not errors, errors
    assert sum(created) == 40  # each task was inserted by exactly one of the 8 syncs
    with session_scope() as session:
        assert session.query(db.CrmTask).count() == 40
        assert session.query(db.CrmTask.dedupe_key).distinct().count() == 40
