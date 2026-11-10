"""Read an edited workbook and decide what may be written back (pure: no database).

Three steps, each testable without a database:

1. :func:`open_workbook` - refuse anything that is not a plain .xlsx of sane size.
2. :func:`parse_workbook` - check the signature of the hidden baseline and read the
   editable cells of the Leads, Tasks and Duplicates sheets.
3. :func:`plan_import` - compare, per field, what the person typed, what the
   workbook showed them (the signed baseline) and what the database holds now.

The per-field rule that makes this safe:

    typed == shown            -> they did not touch it: ignore (even if the data moved)
    typed == database now     -> already applied: nothing to do
    database now == shown     -> nobody else changed it: apply
    otherwise                 -> someone else changed it since the export: CONFLICT,
                                 keep the database value and say so

Nothing is ever written over a value the person did not see.
"""

from __future__ import annotations

import hashlib
import io
import re
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from openpyxl import load_workbook
from openpyxl.utils.datetime import from_excel

from app.crm.excel import baseline as bl
from app.crm.excel import schema as sc
from app.crm.excel import signing
from app.crm.excel.errors import ImportRejected, WorkbookRejected
from app.crm.excel.meta import Meta, read_meta, verify_meta
from app.crm.excel.schema import Column
from app.crm.excel.text import CELL_LIMIT, LEAD_PREFIX, clean, fingerprint, parse_code

MAX_UNCOMPRESSED_FACTOR = 12  # a legitimate workbook compresses roughly 5-10x; far beyond that is a zip bomb
MAX_ZIP_ENTRIES = 300
MIN_DATE, MAX_DATE = date(2000, 1, 1), date(2100, 12, 31)
MAX_REPORT_ROWS = 500
_FORBIDDEN_PARTS = ("vbaProject.bin", "xl/externalLinks/", "xl/embeddings/", "xl/activeX/")


# ---------------------------------------------------------------------------
# 1. Safe open
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Limits:
    max_bytes: int = 10 * 1024 * 1024
    max_rows: int = 10_000


def open_workbook(blob: bytes, limits: Limits):
    """A read-only workbook, after refusing anything that is not a plain .xlsx."""
    if not blob:
        raise ImportRejected("The file is empty.")
    if len(blob) > limits.max_bytes:
        raise ImportRejected(f"The file is larger than the {limits.max_bytes // (1024 * 1024)} MB limit.")
    if blob[:4] != b"PK\x03\x04":
        raise ImportRejected("This is not an .xlsx workbook. Save the file as Excel Workbook (.xlsx).")
    try:
        archive = zipfile.ZipFile(io.BytesIO(blob))
        entries = archive.infolist()
    except zipfile.BadZipFile:
        raise ImportRejected("The file is damaged or is not a real .xlsx workbook.") from None
    if len(entries) > MAX_ZIP_ENTRIES:
        raise ImportRejected("The workbook has an unexpected structure.")
    if sum(entry.file_size for entry in entries) > max(len(blob) * MAX_UNCOMPRESSED_FACTOR, 5 * 1024 * 1024):
        raise ImportRejected("The workbook expands to an unreasonable size.")
    for entry in entries:
        name = entry.filename
        if name.startswith("/") or ".." in name.split("/"):
            raise ImportRejected("The workbook has an unexpected structure.")
        if any(part in name for part in _FORBIDDEN_PARTS):
            raise ImportRejected("Macros, external links and embedded objects are not accepted. Save a plain .xlsx.")
    try:
        return load_workbook(io.BytesIO(blob), read_only=True, data_only=True, keep_vba=False)
    except Exception:
        raise ImportRejected("The workbook could not be read. Export a fresh file and try again.") from None


# ---------------------------------------------------------------------------
# 2. Parse
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RawRow:
    row: int
    cells: dict[str, Any]


@dataclass
class Parsed:
    meta: Meta
    leads: list[RawRow] = field(default_factory=list)
    tasks: list[RawRow] = field(default_factory=list)
    duplicates: list[RawRow] = field(default_factory=list)

    @property
    def export_id(self) -> str:
        return self.meta.export_id


def _norm_header(value: Any) -> str:
    return " ".join(str(value or "").split())


def _read_sheet(workbook: Any, sheet: str, max_rows: int) -> list[RawRow]:
    if sheet not in workbook.sheetnames:
        raise ImportRejected(f"The sheet '{sheet}' is missing. Do not delete or rename sheets.")
    columns = sc.COLUMNS_BY_SHEET[sheet]
    rows = workbook[sheet].iter_rows(values_only=True)
    header = next(rows, None)
    if header is None:
        raise ImportRejected(f"The sheet '{sheet}' is empty.")
    position = {_norm_header(value): index for index, value in enumerate(header) if value is not None}
    missing = [column.header for column in columns if _norm_header(column.header) not in position]
    if missing:
        raise ImportRejected(f"On '{sheet}' these columns are missing or were renamed: {', '.join(missing[:4])}. Do not rename or delete columns.")

    result: list[RawRow] = []
    for number, values in enumerate(rows, start=2):
        if number - 1 > max_rows:
            raise ImportRejected(f"'{sheet}' has more than {max_rows} rows.")
        cells = {column.key: (values[position[_norm_header(column.header)]] if position[_norm_header(column.header)] < len(values) else None) for column in columns}
        if all(value is None or str(value).strip() == "" for value in cells.values()):
            continue
        result.append(RawRow(number, cells))
    return result


def parse_workbook(workbook: Any, *, key: bytes, limits: Limits = Limits()) -> Parsed:
    """Verify the baseline signature and read the three editable sheets."""
    meta = read_meta(workbook)
    verify_meta(meta, key)
    return Parsed(
        meta=meta,
        leads=_read_sheet(workbook, sc.LEADS, limits.max_rows),
        tasks=_read_sheet(workbook, sc.TASKS, limits.max_rows),
        duplicates=_read_sheet(workbook, sc.DUPLICATES, limits.max_rows),
    )


# ---------------------------------------------------------------------------
# Cell interpretation
# ---------------------------------------------------------------------------


def parse_date_cell(value: Any) -> tuple[str, str | None]:
    """``(ISO date or "", error)``; a blank cell is a valid "no date"."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return "", None
    parsed: date | None = None
    if isinstance(value, datetime):
        parsed = value.date()
    elif isinstance(value, date):
        parsed = value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            parsed = from_excel(value).date()
        except Exception:
            parsed = None
    elif isinstance(value, str):
        text = value.strip()
        for pattern in (r"^(\d{4})-(\d{1,2})-(\d{1,2})", r"^(\d{4})/(\d{1,2})/(\d{1,2})"):
            match = re.match(pattern, text)
            if match:
                try:
                    parsed = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
                except ValueError:
                    parsed = None
                break
        else:
            match = re.match(r"^(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})$", text)
            if match:
                try:
                    parsed = date(int(match.group(3)), int(match.group(2)), int(match.group(1)))  # day first, as written in Egypt
                except ValueError:
                    parsed = None
    if parsed is None:
        return "", f"'{str(value)[:30]}' is not a date. Use a date such as 2026-12-31."
    if not MIN_DATE <= parsed <= MAX_DATE:
        return "", f"{parsed.isoformat()} is outside the supported range."
    return parsed.isoformat(), None


def _agent_cell(value: Any) -> str:
    return " ".join(str(value or "").split())[:80]


def _display(value: Any, limit: int = 80) -> str:
    text = clean(value).replace("\n", " ").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ---------------------------------------------------------------------------
# 3. Plan
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LeadState:
    pk: int
    uuid: str
    status: str
    agent: str
    next_iso: str
    last_iso: str  # what the workbook showed: the stored date, else the date derived from handoffs
    notes: str
    closed: bool


@dataclass(frozen=True)
class TaskState:
    pk: int
    uuid: str
    lead_pk: int
    task_type: str
    description: str
    agent: str
    due_iso: str
    priority: str
    status: str
    source: str


@dataclass(frozen=True)
class DuplicateState:
    key: str
    customer_a: int
    customer_b: int
    decision: str  # "" when nobody decided
    reviewed_by: str


@dataclass(frozen=True)
class Context:
    now: datetime
    timezone: str
    agents: Sequence[str]  # configured roster (empty: any name is accepted)
    known_agents: Sequence[str]  # roster plus names already stored in the database
    leads: Mapping[str, LeadState]  # by uuid
    leads_by_pk: Mapping[int, LeadState]
    tasks: Mapping[str, TaskState]  # by uuid
    duplicates: Mapping[str, DuplicateState]  # by "a:b"

    @property
    def today(self) -> date:
        return self.now.astimezone(ZoneInfo(self.timezone)).date()


@dataclass
class FieldChange:
    field: str
    before: str
    after: str

    def as_dict(self) -> dict[str, str]:
        return {"field": self.field, "from": self.before, "to": self.after}


@dataclass
class RowReport:
    sheet: str
    row: int
    ref: str
    result: str = "unchanged"  # unchanged | applied | conflict | partial | error | duplicate
    changes: list[FieldChange] = field(default_factory=list)
    conflicts: list[dict[str, str]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def finish(self) -> None:
        if self.result == "duplicate":
            return
        if self.errors and not self.changes:
            self.result = "error"
        elif self.conflicts and not self.changes:
            self.result = "conflict"
        elif self.changes and (self.errors or self.conflicts):
            self.result = "partial"
        elif self.changes:
            self.result = "applied"
        else:
            self.result = "unchanged"

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"sheet": self.sheet, "row": self.row, "ref": self.ref, "result": self.result}
        if self.changes:
            out["changes"] = [change.as_dict() for change in self.changes]
        for key in ("conflicts", "errors", "warnings"):
            if getattr(self, key):
                out[key] = getattr(self, key)
        return out


@dataclass
class Operation:
    kind: str  # lead | task_update | task_create | duplicate
    row: int
    target: Any  # lead uuid | task uuid | lead pk | pair key
    changes: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Plan:
    operations: list[Operation] = field(default_factory=list)
    reports: list[RowReport] = field(default_factory=list)

    def summary(self) -> dict[str, int]:
        count = lambda result: sum(1 for report in self.reports if report.result == result)  # noqa: E731
        return {
            "rows_read": len(self.reports),
            "applied": count("applied") + count("partial"),
            "unchanged": count("unchanged"),
            "conflicts": sum(len(report.conflicts) for report in self.reports),
            "errors": sum(len(report.errors) for report in self.reports),
            "warnings": sum(len(report.warnings) for report in self.reports),
            "duplicates_skipped": count("duplicate"),
            "fields_changed": sum(len(report.changes) for report in self.reports),
        }

    @property
    def has_problems(self) -> bool:
        return any(report.errors or report.conflicts for report in self.reports)

    def report_rows(self) -> list[dict[str, Any]]:
        interesting = [report.as_dict() for report in self.reports if report.result != "unchanged" or report.warnings]
        return interesting[:MAX_REPORT_ROWS]


def decide(typed: Any, shown: Any, current: Any) -> str:
    """untouched | noop | apply | conflict - the per-field rule described in the module docstring."""
    if typed == shown:
        return "untouched"
    if typed == current:
        return "noop"
    if current == shown:
        return "apply"
    return "conflict"


def _agent_problem(name: str, ctx: Context) -> str | None:
    if not name or not ctx.agents:
        return None
    if name.casefold() not in {known.casefold() for known in ctx.known_agents}:
        return f"'{name}' is not in the agents list."
    return None


def _canonical_agent(name: str, ctx: Context) -> str:
    for known in ctx.known_agents:
        if known.casefold() == name.casefold():
            return known
    return name


def plan_import(parsed: Parsed, ctx: Context) -> Plan:
    plan = Plan()
    _plan_leads(parsed, ctx, plan)
    _plan_tasks(parsed, ctx, plan)
    _plan_duplicates(parsed, ctx, plan)
    for report in plan.reports:
        report.finish()
    return plan


# ----- leads -----------------------------------------------------------------


def _plan_leads(parsed: Parsed, ctx: Context, plan: Plan) -> None:
    baselines = parsed.meta.baseline("lead")
    local_today = ctx.today
    for raw in parsed.leads:
        uuid = str(raw.cells["lead_uuid"] or "").strip()
        report = RowReport(sc.LEADS, raw.row, str(raw.cells["lead_ref"] or "").strip())
        plan.reports.append(report)
        base, state = baselines.get(uuid), ctx.leads.get(uuid)
        if base is None:
            report.errors.append("This row is not part of the exported snapshot. Export a fresh file.")
            continue
        if state is None:
            report.errors.append("This lead no longer exists.")
            continue

        changes: dict[str, Any] = {}
        status_label = sc.label(sc.LEAD_STATUS, base["status"])

        # status ---------------------------------------------------------
        typed_raw = raw.cells["status"]
        typed = sc.parse_choice(sc.LEAD_STATUS, typed_raw)
        if typed is None and _norm_header(typed_raw) == _norm_header(status_label):
            typed = base["status"]  # an unrecognised status shown as-is is not an edit
        if typed is None or typed == "":
            report.errors.append(f"Status: '{_display(typed_raw, 30)}' is not a valid status.")
        else:
            verdict = decide(typed, base["status"], state.status)
            if verdict == "conflict":
                report.conflicts.append({"field": "status", "yours": sc.label(sc.LEAD_STATUS, typed), "database": sc.label(sc.LEAD_STATUS, state.status)})
            elif verdict == "apply":
                if typed not in sc.STAFF_STATUSES:
                    report.errors.append(f"Status: '{sc.label(sc.LEAD_STATUS, typed)}' is set by the system and cannot be chosen by hand.")
                elif not sc.can_set_status(state.status, typed):
                    report.errors.append(f"Status: a lead cannot go from '{sc.label(sc.LEAD_STATUS, state.status)}' to '{sc.label(sc.LEAD_STATUS, typed)}'.")
                else:
                    changes["status"] = typed
                    report.changes.append(FieldChange("status", sc.label(sc.LEAD_STATUS, state.status), sc.label(sc.LEAD_STATUS, typed)))

        # assigned agent ----------------------------------------------------
        typed_agent = _agent_cell(raw.cells["assigned_agent"])
        verdict = decide(typed_agent.casefold(), base["agent"].casefold(), state.agent.casefold())
        if verdict == "conflict":
            report.conflicts.append({"field": "assigned_agent", "yours": typed_agent, "database": state.agent})
        elif verdict == "apply":
            problem = _agent_problem(typed_agent, ctx)
            if problem:
                report.errors.append(f"Assigned agent: {problem}")
            else:
                canonical = _canonical_agent(typed_agent, ctx)
                changes["assigned_agent"] = canonical
                report.changes.append(FieldChange("assigned_agent", state.agent, canonical))

        # dates -------------------------------------------------------------
        for column, key, label in (("next_followup", "next", "Next follow-up"), ("last_contact", "last", "Last contact")):
            typed_date, error = parse_date_cell(raw.cells[column])
            if error and _display(raw.cells[column]) != base[key]:
                report.errors.append(f"{label}: {error}")
                continue
            if error:  # unreadable but identical to what was shown: untouched
                continue
            current = state.next_iso if key == "next" else state.last_iso
            verdict = decide(typed_date, base[key], current)
            if verdict == "conflict":
                report.conflicts.append({"field": column, "yours": typed_date, "database": current})
            elif verdict == "apply":
                if key == "last" and typed_date and date.fromisoformat(typed_date) > local_today:
                    report.errors.append("Last contact: a contact date cannot be in the future.")
                    continue
                if key == "next" and typed_date and date.fromisoformat(typed_date) < local_today:
                    report.warnings.append("Next follow-up is in the past, so it will show as overdue.")
                changes[column] = typed_date
                report.changes.append(FieldChange(column, current, typed_date))

        # notes ---------------------------------------------------------------
        typed_notes = clean(raw.cells["notes"]).strip()
        if base.get("notes_cut"):
            if typed_notes != clean(state.notes).strip()[: len(typed_notes)] and fingerprint(typed_notes) != base["notes"]:
                report.warnings.append("Notes are too long to edit in Excel; this edit was ignored. Edit them in the system.")
        else:
            verdict = decide(fingerprint(typed_notes), base["notes"], fingerprint(state.notes))
            if verdict == "conflict":
                report.conflicts.append({"field": "notes", "yours": _display(typed_notes), "database": _display(state.notes)})
            elif verdict == "apply":
                if len(typed_notes) > CELL_LIMIT:
                    report.errors.append("Notes are too long.")
                else:
                    changes["notes"] = typed_notes
                    report.changes.append(FieldChange("notes", _display(state.notes, 40), _display(typed_notes, 40)))

        if not changes:
            continue
        extra: dict[str, Any] = {}
        if changes.get("status") in ("contacted", "negotiation") and "last_contact" not in changes and not state.last_iso:
            extra["auto_last_contact"] = local_today.isoformat()
            report.warnings.append("Last contact was empty, so it was set to today.")
        if changes.get("status") in ("won", "lost"):
            extra["cancel_system_tasks"] = True
        plan.operations.append(Operation("lead", raw.row, uuid, changes, extra))


# ----- tasks -----------------------------------------------------------------


def _task_cells(raw: RawRow) -> dict[str, Any]:
    return {
        "type": sc.parse_choice(sc.TASK_TYPE, raw.cells["task_type"]),
        "desc": clean(raw.cells["description"]).strip(),
        "agent": _agent_cell(raw.cells["assigned_agent"]),
        "priority": sc.parse_choice(sc.PRIORITY, raw.cells["priority"]),
        "status": sc.parse_choice(sc.TASK_STATUS, raw.cells["status"]),
    }


def staff_task_key(export_id: str, lead_pk: int, task_type: str, description: str, due: str) -> str:
    """Same file + same content -> same key, so re-uploading a file cannot create a task twice."""
    digest = hashlib.sha256("|".join((export_id, str(lead_pk), task_type, " ".join(description.split()).casefold(), due)).encode()).hexdigest()
    return f"staff:{digest[:32]}"


def _plan_tasks(parsed: Parsed, ctx: Context, plan: Plan) -> None:
    baselines = parsed.meta.baseline("task")
    for raw in parsed.tasks:
        uuid = str(raw.cells["task_uuid"] or "").strip()
        report = RowReport(sc.TASKS, raw.row, str(raw.cells["task_ref"] or "").strip() or "(new)")
        plan.reports.append(report)
        cells = _task_cells(raw)
        due, due_error = parse_date_cell(raw.cells["due"])

        if not uuid:
            _plan_new_task(raw, cells, due, due_error, parsed, ctx, plan, report)
            continue
        base, state = baselines.get(uuid), ctx.tasks.get(uuid)
        if base is None:
            report.errors.append("This row is not part of the exported snapshot. Export a fresh file.")
            continue
        if state is None:
            report.errors.append("This task no longer exists.")
            continue

        changes: dict[str, Any] = {}

        def check(name: str, typed: Any, shown: Any, current: Any, validate=None, show=str) -> None:
            verdict = decide(typed, shown, current)
            if verdict == "conflict":
                report.conflicts.append({"field": name, "yours": show(typed), "database": show(current)})
            elif verdict == "apply":
                problem = validate(typed) if validate else None
                if problem:
                    report.errors.append(f"{name}: {problem}")
                else:
                    changes[name] = typed
                    report.changes.append(FieldChange(name, show(current), show(typed)))

        label_of = lambda vocab: (lambda key: sc.label(vocab, key))  # noqa: E731
        if cells["type"] is None and _norm_header(raw.cells["task_type"]) == _norm_header(sc.label(sc.TASK_TYPE, base["type"])):
            cells["type"] = base["type"]
        if cells["type"] is None:
            report.errors.append(f"task_type: '{_display(raw.cells['task_type'], 30)}' is not a valid task type.")
        else:
            check("task_type", cells["type"], base["type"], state.task_type, show=label_of(sc.TASK_TYPE))

        desc_problem = (lambda text: "The description cannot be empty." if not text else ("The description is too long (500 characters)." if len(text) > 500 else None))
        check("description", fingerprint(cells["desc"]), base["desc"], fingerprint(state.description),
              validate=lambda _fp: desc_problem(cells["desc"]), show=lambda _fp: _display(cells["desc"] if _fp == fingerprint(cells["desc"]) else state.description, 40))
        if "description" in changes:
            changes["description"] = cells["desc"]

        check("assigned_agent", cells["agent"].casefold(), base["agent"].casefold(), state.agent.casefold(),
              validate=lambda _n: _agent_problem(cells["agent"], ctx), show=lambda _n: cells["agent"] if _n == cells["agent"].casefold() else state.agent)
        if "assigned_agent" in changes:
            changes["assigned_agent"] = _canonical_agent(cells["agent"], ctx)

        if due_error and _display(raw.cells["due"]) != base["due"]:
            report.errors.append(f"due: {due_error}")
        elif not due_error:
            check("due", due, base["due"], state.due_iso)

        for name, vocab_key, vocab, base_key, current in (("priority", "priority", sc.PRIORITY, "priority", state.priority), ("status", "status", sc.TASK_STATUS, "status", state.status)):
            typed = cells[vocab_key]
            if typed is None and _norm_header(raw.cells[vocab_key]) == _norm_header(sc.label(vocab, base[base_key])):
                typed = base[base_key]
            if typed is None:
                report.errors.append(f"{name}: '{_display(raw.cells[vocab_key], 30)}' is not valid.")
            else:
                check(name, typed, base[base_key], current, show=label_of(vocab))

        if changes:
            plan.operations.append(Operation("task_update", raw.row, uuid, changes, {"lead_pk": state.lead_pk}))


def _plan_new_task(raw: RawRow, cells: dict[str, Any], due: str, due_error: str | None, parsed: Parsed, ctx: Context, plan: Plan, report: RowReport) -> None:
    lead_pk = parse_code(LEAD_PREFIX, raw.cells["lead_ref"])
    lead = ctx.leads_by_pk.get(lead_pk) if lead_pk else None
    if lead is None:
        report.errors.append(f"New task: Lead ID '{_display(raw.cells['lead_ref'], 20)}' was not found (write it like L-000123).")
    elif lead.closed:
        report.errors.append("New task: that lead is closed.")
    if cells["type"] is None:
        report.errors.append("New task: choose a task type from the list.")
    if not cells["desc"]:
        report.errors.append("New task: write a description.")
    elif len(cells["desc"]) > 500:
        report.errors.append("New task: the description is too long (500 characters).")
    if due_error:
        report.errors.append(f"New task: {due_error}")
    priority = cells["priority"] or ("normal" if not _display(raw.cells["priority"]) else None)
    status = cells["status"] or ("open" if not _display(raw.cells["status"]) else None)
    if priority is None:
        report.errors.append("New task: the priority is not valid.")
    if status is None:
        report.errors.append("New task: the status is not valid.")
    problem = _agent_problem(cells["agent"], ctx)
    if problem:
        report.errors.append(f"New task: {problem}")
    if report.errors or lead is None:
        return
    key = staff_task_key(parsed.export_id, lead.pk, cells["type"], cells["desc"], due)
    fields = {
        "task_type": cells["type"], "description": cells["desc"], "assigned_agent": _canonical_agent(cells["agent"], ctx) or None,
        "due": due, "priority": priority, "status": status,
    }
    plan.operations.append(Operation("task_create", raw.row, lead.pk, fields, {"dedupe_key": key}))
    report.changes.append(FieldChange("new task", "", f"{sc.label(sc.TASK_TYPE, cells['type'])} for {report.ref if report.ref != '(new)' else 'L-' + str(lead.pk).zfill(6)}"))


# ----- duplicates -------------------------------------------------------------


def _plan_duplicates(parsed: Parsed, ctx: Context, plan: Plan) -> None:
    baselines = parsed.meta.baseline("dup")
    for raw in parsed.duplicates:
        key = str(raw.cells["pair_key"] or "").strip()
        report = RowReport(sc.DUPLICATES, raw.row, key)
        plan.reports.append(report)
        base, state = baselines.get(key), ctx.duplicates.get(key)
        if base is None:
            report.errors.append("This row is not part of the exported snapshot. Export a fresh file.")
            continue
        if state is None:
            report.errors.append("One of these customers no longer exists.")
            continue

        decision = sc.parse_choice(sc.DUPLICATE_DECISION, raw.cells["decision"])
        if decision is None and _norm_header(raw.cells["decision"]) == _norm_header(sc.label(sc.DUPLICATE_DECISION, base["decision"])):
            decision = base["decision"]
        agent = _agent_cell(raw.cells["reviewed_by"])
        changes: dict[str, Any] = {}
        if decision is None:
            report.errors.append(f"Decision: '{_display(raw.cells['decision'], 30)}' is not valid.")
            continue
        for name, typed, shown, current in (("decision", decision, base["decision"], state.decision), ("reviewed_by", agent.casefold(), base["by"].casefold(), state.reviewed_by.casefold())):
            verdict = decide(typed, shown, current)
            if verdict == "conflict":
                report.conflicts.append({"field": name, "yours": str(typed), "database": str(current)})
            elif verdict == "apply":
                if name == "reviewed_by" and _agent_problem(agent, ctx):
                    report.errors.append(f"Reviewed by: {_agent_problem(agent, ctx)}")
                    continue
                changes[name] = decision if name == "decision" else _canonical_agent(agent, ctx)
                report.changes.append(FieldChange(name, sc.label(sc.DUPLICATE_DECISION, state.decision) if name == "decision" else state.reviewed_by,
                                                  sc.label(sc.DUPLICATE_DECISION, decision) if name == "decision" else agent))
        if changes:
            changes.setdefault("decision", state.decision)
            changes.setdefault("reviewed_by", state.reviewed_by)
            plan.operations.append(Operation("duplicate", raw.row, key, changes, {"a": state.customer_a, "b": state.customer_b}))


def local_midnight_utc(iso_date: str, tz_name: str) -> datetime | None:
    """The start of a local calendar day, as the UTC moment the database stores."""
    if not iso_date:
        return None
    day = date.fromisoformat(iso_date)
    return datetime(day.year, day.month, day.day, tzinfo=ZoneInfo(tz_name)).astimezone(timezone.utc)
