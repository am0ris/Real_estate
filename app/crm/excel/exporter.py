"""Render an :class:`ExportData` snapshot as the sales team's .xlsx workbook.

Pure: no database, no framework, no globals. The workbook is built in memory
and returned as bytes - nothing is written to disk, which matters on a host
with an ephemeral filesystem and means there is no file to lock or corrupt.

Everything that reaches a cell as text goes through :func:`_put`, because
customers control message and name text and a cell that starts with ``=``
would otherwise be executed as a formula when the file is opened.
"""

from __future__ import annotations

import io
import uuid
from collections import Counter
from datetime import date, datetime, timedelta
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from openpyxl import Workbook
from openpyxl.chart import BarChart, LineChart, Reference
from openpyxl.formatting.rule import FormulaRule
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.workbook.defined_name import DefinedName
from openpyxl.worksheet.datavalidation import DataValidation
from openpyxl.worksheet.table import Table, TableStyleInfo

from app.crm.excel import baseline as bl
from app.crm.excel import schema as sc
from app.crm.excel import signing
from app.crm.excel.data import DuplicateRow, ExportData, InteractionRow, LeadRow, TaskRow, ViewingRow
from app.crm.excel.schema import Column, bi
from app.crm.excel.text import (
    BOOKING_PREFIX,
    CELL_LIMIT,
    INTERACTION_PREFIX,
    LEAD_PREFIX,
    TASK_PREFIX,
    clean,
    code,
    is_formula_like,
)

NEW_TASK_ROWS = 500  # validation (dropdowns) is pre-applied this far below the Tasks table
TOP_LOCATIONS = 10
WEEKS = 8

_EDITABLE_FILL = PatternFill("solid", start_color="FFF2CC")
_READONLY_FILL = PatternFill("solid", start_color="DDEBF7")
_HEADER_FONT = Font(bold=True, color="1F2937")
_HEADER_BORDER = Border(bottom=Side(style="medium", color="9CA3AF"))
_TITLE_FONT = Font(bold=True, size=16)
_SECTION_FONT = Font(bold=True, size=12, color="1F4E79")
_RED = PatternFill("solid", start_color="F8CBAD", end_color="F8CBAD")
_AMBER = PatternFill("solid", start_color="FFE699", end_color="FFE699")
_BLUE = PatternFill("solid", start_color="BDD7EE", end_color="BDD7EE")
_GREEN = PatternFill("solid", start_color="C6E0B4", end_color="C6E0B4")

_CHOICE_NAMES = {
    "status": "List_Status",
    "agents": "List_Agents",
    "task_type": "List_TaskType",
    "priority": "List_Priority",
    "task_status": "List_TaskStatus",
    "dup_decision": "List_DupDecision",
}

_MIN_DATE_SERIAL = "36526"  # 2000-01-01


def build_workbook(
    data: ExportData,
    *,
    agents: Sequence[str],
    timezone: str,
    signing_key: bytes,
    export_id: str | None = None,
) -> bytes:
    """The complete workbook as .xlsx bytes."""
    return _Builder(data, agents, timezone, signing_key, export_id or uuid.uuid4().hex).build()


class _Builder:
    def __init__(self, data: ExportData, agents: Sequence[str], timezone: str, key: bytes, export_id: str) -> None:
        self.data = data
        self.tz = ZoneInfo(timezone)
        self.tz_name = timezone
        self.key = key
        self.export_id = export_id
        known = {name.casefold() for name in agents}
        extra = sorted(
            {value for value in [lead.assigned_agent for lead in data.leads] + [task.assigned_agent for task in data.tasks] if value and value.casefold() not in known},
            key=str.casefold,
        )
        self.roster = list(agents)  # configured agents: strict dropdown
        self.agents = list(agents) + extra  # plus anyone already assigned in the data
        self.wb = Workbook()
        self.sheets: dict[str, Any] = {}
        self.baseline_rows: list[tuple[str, str, str]] = []
        self.last_row: dict[str, int] = {}

    # ------------------------------------------------------------------ time

    def _local(self, moment: datetime | None) -> datetime | None:
        return moment.astimezone(self.tz).replace(tzinfo=None, microsecond=0) if moment else None

    def _local_date(self, moment: datetime | None) -> date | None:
        return moment.astimezone(self.tz).date() if moment else None

    # ------------------------------------------------------------------ build

    def build(self) -> bytes:
        wb = self.wb
        wb.remove(wb.active)
        for name in (sc.GUIDE, sc.DASHBOARD, sc.LEADS, sc.INTERACTIONS, sc.TASKS, sc.VIEWINGS, sc.DUPLICATES, sc.LISTS, sc.META):
            self.sheets[name] = wb.create_sheet(name)

        self._lists()
        self._leads()
        self._interactions()
        self._tasks()
        self._viewings()
        self._duplicates()
        self._names()
        self._dashboard()
        self._guide()
        self._meta()

        for name, sheet in self.sheets.items():
            sheet.page_setup.orientation = "landscape"
            if name in (sc.DASHBOARD, sc.GUIDE):  # the wide tables would be unreadable squeezed onto one page
                sheet.page_setup.fitToWidth = 1
                sheet.page_setup.fitToHeight = 0
                sheet.sheet_properties.pageSetUpPr.fitToPage = True
        wb.calculation.fullCalcOnLoad = True
        # Open on the dashboard, and select only that tab: a second selected tab
        # makes Excel treat the sheets as a group and apply edits to both.
        wb.active = wb.worksheets.index(self.sheets[sc.DASHBOARD])
        for name, sheet in self.sheets.items():
            sheet.sheet_view.tabSelected = name == sc.DASHBOARD
        buffer = io.BytesIO()
        wb.save(buffer)
        return buffer.getvalue()

    # ------------------------------------------------------------------ cells

    @staticmethod
    def _put(cell: Any, value: Any, column: Column | None = None) -> None:
        if value is None or value == "":
            return
        kind = column.kind if column else "text"
        if kind in ("int", "money"):
            cell.value = float(value) if kind == "money" else int(value)
            cell.number_format = "#,##0" if kind == "money" else "0"
        elif kind == "date":
            cell.value = value
            cell.number_format = "yyyy-mm-dd"
        elif kind == "datetime":
            cell.value = value
            cell.number_format = "yyyy-mm-dd hh:mm"
        else:
            text = clean(value, CELL_LIMIT)
            cell.value = text
            cell.data_type = "s"  # never a formula, whatever the text looks like
            if is_formula_like(text):
                cell.quotePrefix = True
        if column and column.wrap:
            cell.alignment = Alignment(wrap_text=True, vertical="top")

    def _table(
        self,
        sheet: str,
        columns: Sequence[Column],
        rows: Sequence[Mapping[str, Any]],
        *,
        freeze: str,
        min_rows: int = 1,
        spare_rows: int = 0,
    ) -> None:
        ws = self.sheets[sheet]
        for position, column in enumerate(columns, start=1):
            letter = get_column_letter(position)
            header = ws.cell(row=1, column=position, value=column.header)
            header.font = _HEADER_FONT
            header.fill = _EDITABLE_FILL if column.editable else _READONLY_FILL
            header.border = _HEADER_BORDER
            header.alignment = Alignment(wrap_text=True, vertical="center")
            ws.column_dimensions[letter].width = column.width
            if column.hidden:
                ws.column_dimensions[letter].hidden = True
        ws.row_dimensions[1].height = 34

        for row_number, values in enumerate(rows, start=2):
            for position, column in enumerate(columns, start=1):
                self._put(ws.cell(row=row_number, column=position), values.get(column.key), column)

        last = max(len(rows), min_rows) + 1
        self.last_row[sheet] = last
        table = Table(displayName=sc.TABLE_NAMES[sheet], ref=f"A1:{get_column_letter(len(columns))}{last}")
        table.tableStyleInfo = TableStyleInfo(name="TableStyleLight9", showRowStripes=True)
        ws.add_table(table)
        ws.freeze_panes = freeze

        validated_to = last + spare_rows
        for position, column in enumerate(columns, start=1):
            letter = get_column_letter(position)
            span = f"{letter}2:{letter}{validated_to}"
            if column.choices:
                strict = column.choices != "agents" or bool(self.roster)
                validation = DataValidation(
                    type="list",
                    formula1=_CHOICE_NAMES[column.choices],
                    allow_blank=True,
                    showErrorMessage=strict,
                    errorTitle="Not in the list | غير موجود في القائمة",
                    error="Pick a value from the dropdown. | اختر قيمة من القائمة.",
                )
                validation.add(span)
                ws.add_data_validation(validation)
            elif column.kind == "date" and column.editable:
                validation = DataValidation(
                    type="date",
                    operator="greaterThan",
                    formula1=_MIN_DATE_SERIAL,
                    allow_blank=True,
                    showErrorMessage=True,
                    errorTitle="Date | تاريخ",
                    error="Enter a date such as 2026-12-31. | اكتب تاريخ مثل 2026-12-31.",
                )
                validation.add(span)
                ws.add_data_validation(validation)

    def _rule(self, sheet: str, span: str, formula: str, fill: PatternFill) -> None:
        self.sheets[sheet].conditional_formatting.add(span, FormulaRule(formula=[formula], fill=fill))

    def _col(self, sheet: str, key: str) -> str:
        return get_column_letter(sc.column_index(sheet, key))

    # ------------------------------------------------------------------ lists

    def _lists(self) -> None:
        ws = self.sheets[sc.LISTS]
        vocabularies = {
            "List_Status": ("Lead status | الحالة", [sc.LEAD_STATUS[key] for key in sc.STAFF_STATUSES]),
            "List_Agents": ("Agents | المسؤولين", self.agents),
            "List_TaskType": ("Task type | نوع المهمة", list(sc.TASK_TYPE.values())),
            "List_Priority": ("Priority | الأولوية", list(sc.PRIORITY.values())),
            "List_TaskStatus": ("Task status | حالة المهمة", list(sc.TASK_STATUS.values())),
            "List_DupDecision": ("Decision | القرار", list(sc.DUPLICATE_DECISION.values())),
        }
        for position, (name, (title, values)) in enumerate(vocabularies.items(), start=1):
            letter = get_column_letter(position)
            ws.cell(row=1, column=position, value=title).font = _HEADER_FONT
            ws.column_dimensions[letter].width = 32
            for row, value in enumerate(values, start=2):
                self._put(ws.cell(row=row, column=position), value)
            end = max(2, len(values) + 1)
            self.wb.defined_names[name] = DefinedName(name, attr_text=f"{sc.LISTS}!${letter}$2:${letter}${end}")

    # ------------------------------------------------------------------ sheets

    def _leads(self) -> None:
        rows = []
        for lead in self.data.leads:
            rows.append(self._lead_values(lead))
            self.baseline_rows.append(
                (
                    "lead",
                    lead.uuid,
                    signing.encode_row(
                        bl.lead_baseline(
                            status=lead.status,
                            assigned_agent=lead.assigned_agent,
                            next_followup_at=lead.next_followup_at,
                            last_contact=lead.last_contact_at,
                            notes=lead.notes,
                            tz=self.tz,
                        )
                    ),
                )
            )
        self._table(sc.LEADS, sc.LEAD_COLUMNS, rows, freeze="E2")

        last = self.last_row[sc.LEADS]
        follow = self._col(sc.LEADS, "next_followup")
        status = self._col(sc.LEADS, "status")
        band = self._col(sc.LEADS, "band")
        dup = self._col(sc.LEADS, "possible_duplicate")
        handoff = self._col(sc.LEADS, "open_handoff")
        open_only = f'ISERROR(SEARCH("Won",${status}2)),ISERROR(SEARCH("Lost",${status}2)),ISERROR(SEARCH("Closed",${status}2))'
        self._rule(sc.LEADS, f"{follow}2:{follow}{last}", f"AND(ISNUMBER({follow}2),{follow}2<TODAY(),{open_only})", _RED)
        self._rule(sc.LEADS, f"{follow}2:{follow}{last}", f"AND(ISNUMBER({follow}2),{follow}2=TODAY())", _AMBER)
        self._rule(sc.LEADS, f"{band}2:{band}{last}", f'ISNUMBER(SEARCH("Hot",{band}2))', _RED)
        self._rule(sc.LEADS, f"{band}2:{band}{last}", f'ISNUMBER(SEARCH("Warm",{band}2))', _AMBER)
        self._rule(sc.LEADS, f"{band}2:{band}{last}", f'ISNUMBER(SEARCH("Cold",{band}2))', _BLUE)
        self._rule(sc.LEADS, f"{dup}2:{dup}{last}", f'ISNUMBER(SEARCH("Yes",{dup}2))', _AMBER)
        self._rule(sc.LEADS, f"{handoff}2:{handoff}{last}", f'ISNUMBER(SEARCH("Yes",{handoff}2))', _RED)

    def _lead_values(self, lead: LeadRow) -> dict[str, Any]:
        transaction = sc.normalize_transaction(lead.transaction_type)
        return {
            "lead_ref": code(LEAD_PREFIX, lead.pk),
            "lead_uuid": lead.uuid,
            "name": lead.name,
            "phone": lead.phone,
            "status": sc.label(sc.LEAD_STATUS, lead.status),
            "assigned_agent": lead.assigned_agent,
            "next_followup": self._local_date(lead.next_followup_at),
            "last_contact": self._local_date(lead.last_contact_at),
            "notes": lead.notes,
            "next_task": lead.next_task,
            "score": lead.score,
            "band": sc.label(sc.BAND, lead.band),
            "qualified": sc.YES if lead.qualified else sc.NO,
            "phone_verified": sc.YES if lead.phone_verified else sc.NO,
            "email": lead.email,
            "channel": lead.channel,
            "created": self._local_date(lead.created_at),
            "language": lead.language,
            "transaction": sc.label(sc.TRANSACTION, transaction) if transaction else (lead.transaction_type or ""),
            "location": lead.location,
            "compound": lead.compound,
            "property_type": lead.property_type,
            "budget_min": lead.budget_min,
            "budget_max": lead.budget_max,
            "bedrooms": lead.bedrooms,
            "requirements": lead.requirements,
            "property_ref": lead.property_ref,
            "last_message": self._local(lead.last_message_at),
            "next_viewing": self._local(lead.next_viewing_at),
            "open_handoff": sc.YES if lead.open_handoff else sc.NO,
            "person": lead.person_key,
            "possible_duplicate": sc.YES if lead.possible_duplicate else sc.NO,
        }

    def _interactions(self) -> None:
        rows = [
            {
                "interaction_ref": code(INTERACTION_PREFIX, item.pk),
                "lead_ref": code(LEAD_PREFIX, item.lead_pk) if item.lead_pk else "",
                "customer": item.customer,
                "when": self._local(item.created_at),
                "channel": item.channel,
                "modality": item.modality,
                "customer_message": item.customer_message,
                "assistant_response": item.assistant_response,
            }
            for item in self.data.interactions
        ]
        self._table(sc.INTERACTIONS, sc.INTERACTION_COLUMNS, rows, freeze="D2")

    def _tasks(self) -> None:
        rows = []
        for task in self.data.tasks:
            rows.append(
                {
                    "task_ref": code(TASK_PREFIX, task.pk),
                    "task_uuid": task.uuid,
                    "lead_ref": code(LEAD_PREFIX, task.lead_pk),
                    "customer": task.customer,
                    "phone": task.phone,
                    "task_type": sc.label(sc.TASK_TYPE, task.task_type),
                    "description": task.description,
                    "assigned_agent": task.assigned_agent,
                    "due": self._local_date(task.due_at),
                    "priority": sc.label(sc.PRIORITY, task.priority),
                    "status": sc.label(sc.TASK_STATUS, task.status),
                    "completed": self._local_date(task.completed_at),
                    "source": task.source,
                    "created": self._local_date(task.created_at),
                }
            )
            self.baseline_rows.append(
                (
                    "task",
                    task.uuid,
                    signing.encode_row(
                        bl.task_baseline(
                            task_type=task.task_type,
                            description=task.description,
                            assigned_agent=task.assigned_agent,
                            due_at=task.due_at,
                            priority=task.priority,
                            status=task.status,
                            tz=self.tz,
                        )
                    ),
                )
            )
        self._table(sc.TASKS, sc.TASK_COLUMNS, rows, freeze="D2", spare_rows=NEW_TASK_ROWS)

        last = self.last_row[sc.TASKS]
        due, status = self._col(sc.TASKS, "due"), self._col(sc.TASKS, "status")
        active = f"OR(${status}2={_quote(sc.TASK_STATUS['open'])},${status}2={_quote(sc.TASK_STATUS['in_progress'])})"
        span = f"{due}2:{due}{last + NEW_TASK_ROWS}"
        self._rule(sc.TASKS, span, f"AND(ISNUMBER({due}2),{due}2<TODAY(),{active})", _RED)
        self._rule(sc.TASKS, span, f"AND(ISNUMBER({due}2),{due}2=TODAY(),{active})", _AMBER)
        done = f"${status}2={_quote(sc.TASK_STATUS['done'])}"
        self._rule(sc.TASKS, f"{status}2:{status}{last + NEW_TASK_ROWS}", done, _GREEN)

    def _viewings(self) -> None:
        rows = [
            {
                "booking_ref": code(BOOKING_PREFIX, item.pk),
                "lead_ref": code(LEAD_PREFIX, item.lead_pk) if item.lead_pk else "",
                "customer": item.customer,
                "phone": item.phone,
                "property_id": item.property_id,
                "starts": self._local(item.starts_at),
                "ends": self._local(item.ends_at),
                "status": sc.label(sc.BOOKING_STATUS, item.status),
                "agent": item.agent,
                "notes": item.notes,
            }
            for item in self.data.viewings
        ]
        self._table(sc.VIEWINGS, sc.VIEWING_COLUMNS, rows, freeze="D2")

    def _duplicates(self) -> None:
        rows = []
        for item in self.data.duplicates:
            rows.append(
                {
                    "pair_key": item.pair_key,
                    "a_name": item.a_name,
                    "a_phone": item.a_phone,
                    "a_channel": item.a_channel,
                    "a_verified": sc.YES if item.a_verified else sc.NO,
                    "b_name": item.b_name,
                    "b_phone": item.b_phone,
                    "b_channel": item.b_channel,
                    "b_verified": sc.YES if item.b_verified else sc.NO,
                    "evidence": sc.label(sc.EVIDENCE, item.evidence),
                    "decision": sc.label(sc.DUPLICATE_DECISION, item.decision),
                    "reviewed_by": item.reviewed_by,
                    "reviewed_at": self._local(item.reviewed_at),
                }
            )
            self.baseline_rows.append(
                ("dup", item.pair_key, signing.encode_row(bl.duplicate_baseline(item.decision, item.reviewed_by)))
            )
        self._table(sc.DUPLICATES, sc.DUPLICATE_COLUMNS, rows, freeze="B2")

    # ------------------------------------------------------------------ names

    def _names(self) -> None:
        """Whole-column names, so dashboard formulas keep working as rows are added."""
        pairs = {
            "Leads_UUID": (sc.LEADS, "lead_uuid"),
            "Leads_Status": (sc.LEADS, "status"),
            "Leads_Channel": (sc.LEADS, "channel"),
            "Leads_Transaction": (sc.LEADS, "transaction"),
            "Leads_Location": (sc.LEADS, "location"),
            "Leads_Qualified": (sc.LEADS, "qualified"),
            "Leads_Band": (sc.LEADS, "band"),
            "Leads_Created": (sc.LEADS, "created"),
            "Tasks_Status": (sc.TASKS, "status"),
            "Tasks_Due": (sc.TASKS, "due"),
            "Viewings_Start": (sc.VIEWINGS, "starts"),
            "Viewings_Status": (sc.VIEWINGS, "status"),
        }
        for name, (sheet, key) in pairs.items():
            letter = self._col(sheet, key)
            self.wb.defined_names[name] = DefinedName(name, attr_text=f"{sheet}!${letter}:${letter}")

    # ------------------------------------------------------------------ dashboard

    def _dashboard(self) -> None:
        ws = self.sheets[sc.DASHBOARD]
        ws.column_dimensions["A"].width = 62
        ws.column_dimensions["B"].width = 14
        ws.column_dimensions["C"].width = 3

        ws["A1"] = bi("CRM Dashboard", "لوحة متابعة العملاء")
        ws["A1"].font = _TITLE_FONT
        generated = self._local(self.data.generated_at)
        ws["A2"] = f"{bi('Snapshot taken', 'وقت التصدير')}: {generated:%Y-%m-%d %H:%M} ({self.tz_name})"
        ws["A3"] = bi("Numbers recalculate when you edit the Leads and Tasks sheets.", "الأرقام بتتحدّث لما تعدّل في شيتي العملاء والمهام.")
        for ref in ("A2", "A3"):
            ws[ref].font = Font(italic=True, color="6B7280")

        row = 5
        ws.cell(row=row, column=1, value=bi("Key figures", "مؤشرات رئيسية")).font = _SECTION_FONT
        row += 1

        label = sc.LEAD_STATUS
        total = "COUNTA(Leads_UUID)-1"
        kpis: list[tuple[str, str, str]] = [
            (bi("Total leads", "إجمالي العملاء المحتملين"), f"={total}", "0"),
            (bi("New leads", "عملاء جدد"), f"=COUNTIF(Leads_Status,{_quote(label['new'])})", "0"),
            (bi("Qualified leads", "عملاء مؤهلين"), f"=COUNTIF(Leads_Qualified,{_quote(sc.YES)})", "0"),
            (bi("Unqualified leads", "عملاء غير مؤهلين"), f"=COUNTIF(Leads_Qualified,{_quote(sc.NO)})", "0"),
            (bi("Hot leads", "عملاء ساخنين"), f"=COUNTIF(Leads_Band,{_quote(sc.BAND['hot'])})", "0"),
            (bi("Warm leads", "عملاء دافئين"), f"=COUNTIF(Leads_Band,{_quote(sc.BAND['warm'])})", "0"),
            (bi("Cold leads", "عملاء باردين"), f"=COUNTIF(Leads_Band,{_quote(sc.BAND['cold'])})", "0"),
            (
                bi("Follow-ups due today", "متابعات مستحقة اليوم"),
                "=" + "+".join(f"COUNTIFS(Tasks_Due,TODAY(),Tasks_Status,{_quote(sc.TASK_STATUS[key])})" for key in sc.ACTIVE_TASK_STATUSES),
                "0",
            ),
            (
                bi("Overdue follow-ups", "متابعات متأخرة"),
                "=" + "+".join(f'COUNTIFS(Tasks_Due,"<"&TODAY(),Tasks_Due,">0",Tasks_Status,{_quote(sc.TASK_STATUS[key])})' for key in sc.ACTIVE_TASK_STATUSES),
                "0",
            ),
            (
                bi("Upcoming viewings", "معاينات قادمة"),
                "=" + "+".join(f'COUNTIFS(Viewings_Start,">="&NOW(),Viewings_Status,{_quote(sc.BOOKING_STATUS[key])})' for key in sc.ACTIVE_BOOKING_STATUSES),
                "0",
            ),
            (
                bi("Conversion rate (won / all leads)", "معدل التحويل (تم التحويل / الكل)"),
                f'=IFERROR(COUNTIF(Leads_Status,{_quote(label["won"])})/({total}),0)',
                "0.0%",
            ),
            (
                bi("Win rate (won / closed)", "معدل النجاح (تم التحويل / المغلق)"),
                f'=IFERROR(COUNTIF(Leads_Status,{_quote(label["won"])})/(COUNTIF(Leads_Status,{_quote(label["won"])})+COUNTIF(Leads_Status,{_quote(label["lost"])})),0)',
                "0.0%",
            ),
        ]
        for text, formula, number_format in kpis:
            ws.cell(row=row, column=1, value=text)
            cell = ws.cell(row=row, column=2, value=formula)
            cell.number_format = number_format
            cell.font = Font(bold=True)
            row += 1
        total_ref = "$B$6"

        def section(title: str) -> int:
            nonlocal row
            row += 1
            ws.cell(row=row, column=1, value=title).font = _SECTION_FONT
            row += 1
            return row

        # ---- by status
        status_start = section(bi("Leads by status", "العملاء حسب الحالة"))
        for key, shown in sc.LEAD_STATUS.items():
            ws.cell(row=row, column=1, value=shown)
            ws.cell(row=row, column=2, value=f"=COUNTIF(Leads_Status,$A{row})")
            row += 1
        status_end = row - 1

        # ---- by channel
        channel_start = section(bi("Leads by channel", "العملاء حسب القناة"))
        channels = sorted({lead.channel for lead in self.data.leads if lead.channel}, key=str.casefold) or [""]
        for channel in channels:
            self._put(ws.cell(row=row, column=1), channel or sc.UNKNOWN)
            ws.cell(row=row, column=2, value=f"=COUNTIF(Leads_Channel,$A{row})" if channel else 0)
            row += 1
        channel_end = row - 1

        # ---- by transaction type
        section(bi("Leads by transaction type", "العملاء حسب نوع المعاملة"))
        first = row
        for key in ("sale", "rent"):
            ws.cell(row=row, column=1, value=sc.TRANSACTION[key])
            ws.cell(row=row, column=2, value=f"=COUNTIF(Leads_Transaction,$A{row})")
            row += 1
        ws.cell(row=row, column=1, value=sc.UNKNOWN)
        ws.cell(row=row, column=2, value=f"=MAX(0,{total_ref}-SUM(B{first}:B{row - 1}))")
        row += 1

        # ---- by location
        location_start = section(bi("Leads by location (top 10)", "العملاء حسب الموقع (أعلى 10)"))
        counts = Counter(lead.location for lead in self.data.leads if lead.location)
        top = [name for name, _ in sorted(counts.items(), key=lambda item: (-item[1], item[0].casefold()))[:TOP_LOCATIONS]]
        for name in top:
            self._put(ws.cell(row=row, column=1), name)
            ws.cell(row=row, column=2, value=f"=COUNTIF(Leads_Location,$A{row})")
            row += 1
        location_end = row - 1
        ws.cell(row=row, column=1, value=bi("Other or not specified", "أخرى أو غير محدد"))
        ws.cell(row=row, column=2, value=f"=MAX(0,{total_ref}-SUM(B{location_start}:B{row - 1}))" if top else f"={total_ref}")
        row += 1

        # ---- new leads per week
        week_start = section(bi("New leads per week", "عملاء جدد أسبوعياً"))
        today = self._local_date(self.data.generated_at) or date.today()
        monday = today - timedelta(days=today.weekday())
        for offset in range(WEEKS - 1, -1, -1):
            cell = ws.cell(row=row, column=1, value=monday - timedelta(weeks=offset))
            cell.number_format = "yyyy-mm-dd"
            cell.alignment = Alignment(horizontal="left")
            ws.cell(row=row, column=2, value=f'=COUNTIFS(Leads_Created,">="&$A{row},Leads_Created,"<"&$A{row}+7)')
            row += 1
        week_end = row - 1

        self._chart_bar(ws, bi("Leads by status", "العملاء حسب الحالة"), status_start, status_end, "E5")
        self._chart_bar(ws, bi("Leads by channel", "العملاء حسب القناة"), channel_start, channel_end, "E27")
        if top:
            self._chart_bar(ws, bi("Leads by location", "العملاء حسب الموقع"), location_start, location_end, "E43")
        line = LineChart()
        line.title = bi("New leads per week", "عملاء جدد أسبوعياً")
        line.height, line.width = 7.5, 16
        line.add_data(Reference(ws, min_col=2, min_row=week_start, max_row=week_end))
        line.set_categories(Reference(ws, min_col=1, min_row=week_start, max_row=week_end))
        line.legend = None
        ws.add_chart(line, "E61")

    @staticmethod
    def _chart_bar(ws: Any, title: str, first: int, last: int, anchor: str) -> None:
        chart = BarChart()
        chart.type = "bar"
        chart.title = title
        chart.height, chart.width = max(7.5, 0.55 * (last - first + 1) + 2), 16
        chart.add_data(Reference(ws, min_col=2, min_row=first, max_row=last))
        chart.set_categories(Reference(ws, min_col=1, min_row=first, max_row=last))
        chart.legend = None
        ws.add_chart(chart, anchor)

    # ------------------------------------------------------------------ guide

    def _guide(self) -> None:
        ws = self.sheets[sc.GUIDE]
        ws.column_dimensions["A"].width = 130
        generated = self._local(self.data.generated_at)
        lines: list[tuple[str, bool]] = [
            (bi("CRM workbook guide", "دليل ملف العملاء"), True),
            (f"{bi('Snapshot taken', 'وقت التصدير')}: {generated:%Y-%m-%d %H:%M} ({self.tz_name})", False),
            ("This file is a snapshot of the database. Editing it does not change the database by itself. "
             "Upload it through the import endpoint to apply your changes.", False),
            ("الملف ده نسخة من قاعدة البيانات. التعديل عليه لوحده مابيغيّرش حاجة. ارفعه من خلال خدمة الاستيراد عشان تتسجّل تعديلاتك.", False),
            ("", False),
            (bi("What you can edit", "اللي تقدر تعدّله"), True),
            ("Yellow headers are editable: lead status, assigned agent, next follow-up, last contact and notes on Leads; "
             "type, description, agent, due date, priority and status on Tasks; the decision on Duplicates.", False),
            ("العناوين الصفراء قابلة للتعديل. أي تعديل في عمود أزرق بيتجاهله الاستيراد.", False),
            ("To add a task: add a row on Tasks, fill Lead ID (for example L-000123), type and description.", False),
            ("لإضافة مهمة: أضف صفًا في شيت Tasks واكتب رقم العميل (مثل L-000123) والنوع والوصف.", False),
            ("Do not rename or delete columns. You may sort, filter and reorder rows freely.", False),
            ("", False),
            (bi("Status rules", "قواعد الحالة"), True),
            ("You can set: Contacted, Qualified, Negotiation, Won, Lost. Won leads are final. A Lost lead can be reopened as Contacted. "
             "Viewing scheduled follows the bookings and cannot be set by hand.", False),
            ("", False),
            (bi("Conflicts", "التعارض"), True),
            ("If someone else (or the assistant) changed the same field after this snapshot, the import reports a conflict "
             "and keeps the database value. Export again and redo that edit.", False),
        ]
        if self.data.truncated:
            lines += [("", False), (bi("Not everything is included", "مش كل البيانات موجودة"), True)]
            for name, count in sorted(self.data.truncated.items()):
                lines.append((f"{name}: {count} older rows were left out to keep the file small.", False))
        lines += [("", False), (f"{bi('Interactions', 'التفاعلات')}: last {self.data.interaction_days} days, "
                                f"messages shortened to 300 characters.", False)]
        for row, (text, heading) in enumerate(lines, start=1):
            cell = self.sheets[sc.GUIDE].cell(row=row, column=1)
            self._put(cell, text)
            cell.alignment = Alignment(wrap_text=True, vertical="top")
            if heading:
                cell.font = _SECTION_FONT if row > 1 else _TITLE_FONT

    # ------------------------------------------------------------------ meta

    def _meta(self) -> None:
        ws = self.sheets[sc.META]
        generated_at = self.data.generated_at.isoformat()
        header = signing.make_header(self.export_id, generated_at, self.tz_name)
        signature = signing.sign(self.key, header, self.baseline_rows)
        for row, (key, value) in enumerate(sorted(header.items()), start=1):
            ws.cell(row=row, column=1, value=key)
            ws.cell(row=row, column=2, value=value)
        ws.cell(row=len(header) + 1, column=1, value="signature")
        ws.cell(row=len(header) + 1, column=2, value=signature)
        ws.cell(row=len(header) + 2, column=1, value="rows")
        ws.cell(row=len(header) + 2, column=2, value=len(self.baseline_rows))
        start = len(header) + 4
        for offset, (kind, ident, payload) in enumerate(sorted(self.baseline_rows), start=0):
            ws.cell(row=start + offset, column=1, value=kind)
            ws.cell(row=start + offset, column=2, value=ident)
            cell = ws.cell(row=start + offset, column=3, value=payload)
            cell.data_type = "s"
        ws.sheet_state = "veryHidden"


def _quote(text: str) -> str:
    """A string literal for use inside an Excel formula."""
    return '"' + text.replace('"', '""') + '"'
