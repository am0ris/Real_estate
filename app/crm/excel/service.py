"""Build the workbook for one export request.

The steps are independent transactions on purpose: creating tasks is best
effort (an export still succeeds if it fails), while reading is one consistent
read-only snapshot.

At most one workbook is built at a time per process. The file is assembled in
memory (measured: ~90 MB for 5,000 leads), so concurrent exports on a small
instance could exhaust memory and take the whole API down; a second request
waits briefly and is then told to retry.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from app.crm.excel import signing
from app.crm.excel.errors import WorkbookBusy
from app.crm.excel.exporter import build_workbook
from app.crm.excel.queries import load_export_data
from app.crm.excel.tasks import SyncResult, sync_system_tasks
from app.database.session import session_scope
from app.helpers.config import get_settings

logger = logging.getLogger(__name__)

XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@dataclass(frozen=True)
class ExportResult:
    content: bytes
    filename: str
    summary: dict[str, Any] = field(default_factory=dict)


class CrmWorkbookService:
    def __init__(self, *, wait_seconds: float = 20.0) -> None:
        self._gate = threading.Semaphore(1)
        self._wait = wait_seconds

    def export(self, *, include_closed: bool = True, interaction_days: int = 90, now: datetime | None = None) -> ExportResult:
        settings = get_settings()
        # Fail before doing any work if the file could not be signed.
        key = signing.derive_key(settings.CRM_SIGNING_KEY, settings.ADMIN_API_KEY or settings.CRM_API_KEY)
        if not self._gate.acquire(timeout=self._wait):
            raise WorkbookBusy("Another CRM export is running")
        try:
            now = now or datetime.now(timezone.utc).replace(microsecond=0)
            sync = self._sync_tasks(now, settings.VIEWING_TIMEZONE, settings.CRM_INACTIVE_DAYS)
            with session_scope() as session:
                data = load_export_data(
                    session,
                    now=now,
                    interaction_days=interaction_days,
                    include_closed=include_closed,
                    max_leads=settings.CRM_EXPORT_MAX_LEADS,
                    max_interactions=settings.CRM_EXPORT_MAX_INTERACTIONS,
                )
            content = build_workbook(data, agents=settings.crm_agents, timezone=settings.VIEWING_TIMEZONE, signing_key=key)
        finally:
            self._gate.release()

        local = now.astimezone(ZoneInfo(settings.VIEWING_TIMEZONE))
        summary = {
            "leads": len(data.leads),
            "tasks": len(data.tasks),
            "interactions": len(data.interactions),
            "viewings": len(data.viewings),
            "duplicates": len(data.duplicates),
            "include_closed": include_closed,
            "tasks_created": sync.created,
            "tasks_completed": sync.completed,
            "tasks_cancelled": sync.cancelled,
            "bytes": len(content),
        }
        if data.truncated:
            summary["truncated"] = data.truncated
        return ExportResult(content, f"crm-{local:%Y%m%d-%H%M}.xlsx", summary)

    @staticmethod
    def _sync_tasks(now: datetime, tz_name: str, inactive_days: int) -> SyncResult:
        try:
            with session_scope() as session:
                return sync_system_tasks(session, now=now, timezone=tz_name, inactive_days=inactive_days)
        except Exception:
            # Tasks are a convenience on top of the export, never a reason to fail it.
            logger.exception("CRM task sync failed; exporting without it")
            return SyncResult()


crm_workbook_service = CrmWorkbookService()
