"""Read the workbook's hidden ``_meta`` sheet back (shared by tests and the importer)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from app.crm.excel import schema as sc
from app.crm.excel.errors import WorkbookRejected
from app.crm.excel import signing


@dataclass(frozen=True)
class Meta:
    header: dict[str, str]
    signature: str
    rows: list[tuple[str, str, str]]

    @property
    def export_id(self) -> str:
        return self.header["export_id"]

    def baseline(self, kind: str) -> dict[str, dict[str, Any]]:
        """``id -> baseline dict`` for one kind of row ("lead", "task" or "dup")."""
        return {ident: json.loads(payload) for k, ident, payload in self.rows if k == kind}


def read_meta(workbook: Any) -> Meta:
    """Parse ``_meta`` from an openpyxl workbook. Does not check the signature."""
    if sc.META not in workbook.sheetnames:
        raise WorkbookRejected("This is not a CRM workbook export (no _meta sheet). Export a fresh file and edit that one.")
    header: dict[str, str] = {}
    signature = ""
    rows: list[tuple[str, str, str]] = []
    in_rows = False
    for key, value, extra in workbook[sc.META].iter_rows(min_col=1, max_col=3, values_only=True):
        if key is None and value is None:
            in_rows = in_rows or bool(header)
            continue
        if in_rows:
            rows.append((str(key), str(value), str(extra if extra is not None else "")))
        elif key == "signature":
            signature = str(value or "")
        elif key == "rows":
            continue
        else:
            header[str(key)] = str(value if value is not None else "")
    missing = {"schema_version", "export_id", "generated_at", "timezone"} - set(header)
    if missing or not signature:
        raise WorkbookRejected("The workbook's hidden metadata is incomplete. Export a fresh file.")
    return Meta(header, signature, rows)


def verify_meta(meta: Meta, key: bytes) -> None:
    """Raise :class:`WorkbookRejected` unless the file is current and untampered."""
    if meta.header.get("schema_version") != str(sc.SCHEMA_VERSION):
        raise WorkbookRejected(
            f"This workbook uses layout version {meta.header.get('schema_version')}; the server expects {sc.SCHEMA_VERSION}. Export a fresh file."
        )
    if not signing.verify(key, meta.header, meta.rows, meta.signature):
        raise WorkbookRejected(
            "The workbook's hidden data does not match its signature: it was exported by a different deployment or key, or it was altered. Export a fresh file."
        )
