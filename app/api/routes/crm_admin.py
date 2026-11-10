"""CRM workbook endpoints for the sales team.

``GET /admin/crm/export.xlsx`` returns the CRM as an Excel workbook generated
from PostgreSQL at that moment. Access uses the same ``X-Admin-Key`` header,
rate limit and lock-out as the other operator routes. ``CRM_API_KEY`` lets you
give the sales team a key that opens only these routes; ``ADMIN_API_KEY`` keeps
working here too.
"""

from __future__ import annotations

import hmac
import logging

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from app.api.ratelimit import client_ip, limit_admin, record_admin_failure
from app.api.security import audit
from app.crm.excel.errors import ImportRejected, WorkbookBusy, WorkbookRejected
from app.crm.excel.signing import SigningUnavailable
from app.helpers.config import get_settings

logger = logging.getLogger(__name__)

XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _crm_unavailable(error: ImportError) -> HTTPException:
    """The workbook packages are not installed (or broken) on this server.

    They are imported only when a CRM request arrives, never at start-up, so a
    missing package can only ever disable these two routes - the application and
    the assistant keep running.
    """
    logger.error("CRM workbook support is unavailable: %s", error)
    return HTTPException(
        status_code=503,
        detail="The CRM workbook is unavailable on this server: a required package is missing (openpyxl). Install requirements.txt and redeploy.",
    )


def require_crm_staff(request: Request, x_admin_key: str | None = Header(default=None)) -> None:
    """Accept CRM_API_KEY or ADMIN_API_KEY in X-Admin-Key; disabled when neither is set."""
    settings = get_settings()
    accepted = [key for key in (settings.CRM_API_KEY, settings.ADMIN_API_KEY) if key]
    if not accepted:
        raise HTTPException(status_code=503, detail="CRM endpoints are disabled; set CRM_API_KEY or ADMIN_API_KEY")
    limit_admin(request)
    supplied = (x_admin_key or "").encode()
    matches = [hmac.compare_digest(supplied, key.encode()) for key in accepted]  # every key is checked: no early exit
    if not any(matches):
        record_admin_failure(request)
        logger.warning("Rejected CRM request | client=%s path=%s", client_ip(request), request.url.path)
        raise HTTPException(status_code=401, detail="Invalid admin key")


router = APIRouter(prefix="/admin/crm", tags=["crm"], dependencies=[Depends(require_crm_staff)])


@router.get("/export.xlsx")
async def export_workbook(
    request: Request,
    include_closed: bool = True,
    interaction_days: int = Query(default=90, ge=1, le=365),
) -> Response:
    """The CRM workbook, built from the database right now."""
    try:
        from app.crm.excel.service import crm_workbook_service
    except ImportError as error:
        raise _crm_unavailable(error)
    try:
        result = await run_in_threadpool(
            crm_workbook_service.export, include_closed=include_closed, interaction_days=interaction_days
        )
    except WorkbookBusy:
        raise HTTPException(status_code=429, detail="Another export is running; try again in a moment", headers={"Retry-After": "15"})
    except SigningUnavailable as error:
        raise HTTPException(status_code=503, detail=str(error))
    audit(request, "crm.export", None, result.summary)  # counts only: no customer data in the audit log
    return Response(
        content=result.content,
        media_type=XLSX_MEDIA_TYPE,
        headers={
            "Content-Disposition": f'attachment; filename="{result.filename}"',
            "Cache-Control": "no-store, private",
            "Pragma": "no-cache",
            "X-Content-Type-Options": "nosniff",
        },
    )


_IMPORT_BODY_DOC = {
    "requestBody": {
        "required": True,
        "content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}},
        "description": "The edited .xlsx file itself, sent as the request body (not as a form field).",
    }
}


async def _read_capped(request: Request, limit: int) -> bytes:
    """The request body, refusing to hold more than ``limit`` bytes.

    The body is read here, after ``require_crm_staff`` has run, and as a stream: an
    unauthenticated client cannot make the server buffer or spool anything, and an
    authenticated one cannot send more than the limit.
    """
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise HTTPException(status_code=413, detail=f"The file is larger than the {limit // (1024 * 1024)} MB limit")
    received = bytearray()
    async for chunk in request.stream():
        received += chunk
        if len(received) > limit:
            raise HTTPException(status_code=413, detail=f"The file is larger than the {limit // (1024 * 1024)} MB limit")
    return bytes(received)


@router.post("/import", openapi_extra=_IMPORT_BODY_DOC)
async def import_workbook(
    request: Request,
    dry_run: bool = Query(default=True, description="Preview only. Send dry_run=false to apply."),
    all_or_nothing: bool = Query(default=False, description="Apply nothing if any row has a problem."),
) -> JSONResponse:
    """Check an edited workbook and (with ``dry_run=false``) write the valid changes back.

    Send the .xlsx as the raw request body, for example
    ``curl --data-binary @crm.xlsx -H "X-Admin-Key: ..." -H "Content-Type: application/octet-stream" "…/admin/crm/import"``.

    A field is only written when nobody else changed it since the export; otherwise
    it is reported as a conflict and the database value is kept.
    """
    if (request.headers.get("content-type") or "").lower().startswith("multipart/"):
        raise HTTPException(status_code=415, detail="Send the .xlsx file itself as the request body (curl --data-binary @file.xlsx), not as a multipart form")
    blob = await _read_capped(request, get_settings().CRM_IMPORT_MAX_MB * 1024 * 1024)
    try:
        from app.crm.excel.importer import crm_import_service
    except ImportError as error:
        raise _crm_unavailable(error)
    try:
        result = await run_in_threadpool(crm_import_service.run, blob, dry_run=dry_run, all_or_nothing=all_or_nothing)
    except (ImportRejected, WorkbookRejected) as error:
        raise HTTPException(status_code=400, detail=str(error))
    except WorkbookBusy:
        raise HTTPException(status_code=429, detail="Another import is running; try again in a moment", headers={"Retry-After": "15"})
    except SigningUnavailable as error:
        raise HTTPException(status_code=503, detail=str(error))
    audit(request, "crm.import" if result.applied else "crm.import.preview", None, result.audit_detail)
    return JSONResponse(result.report, headers={"Cache-Control": "no-store, private", "X-Content-Type-Options": "nosniff"})
