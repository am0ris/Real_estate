"""Workbook layout: sheets, columns, vocabularies and the lead-status rules.

Pure data and tiny pure functions; no I/O. Every label is bilingual
("English | Arabic") because the sales team works in both languages.
"""

from __future__ import annotations

from dataclasses import dataclass

SCHEMA_VERSION = 1
SEP = " | "


def bi(en: str, ar: str) -> str:
    return f"{en}{SEP}{ar}"


YES = bi("Yes", "نعم")
NO = bi("No", "لا")
UNKNOWN = bi("Not specified", "غير محدد")

# ---------------------------------------------------------------------------
# Sheets
# ---------------------------------------------------------------------------

GUIDE = "Guide"
DASHBOARD = "Dashboard"
LEADS = "Leads"
INTERACTIONS = "Interactions"
TASKS = "Tasks"
VIEWINGS = "Viewings"
DUPLICATES = "Duplicates"
LISTS = "Lists"
META = "_meta"

TABLE_NAMES = {
    LEADS: "LeadsTable",
    INTERACTIONS: "InteractionsTable",
    TASKS: "TasksTable",
    VIEWINGS: "ViewingsTable",
    DUPLICATES: "DuplicatesTable",
}

# ---------------------------------------------------------------------------
# Vocabularies: stored key -> bilingual label shown in Excel
# ---------------------------------------------------------------------------

LEAD_STATUS = {
    "new": bi("New", "جديد"),
    "qualifying": bi("Qualifying", "قيد التأهيل"),
    "qualified": bi("Qualified", "مؤهل"),
    "contact_requested": bi("Contact requested", "طلب تواصل"),
    "hot": bi("Hot", "ساخن"),
    "viewing_requested": bi("Viewing requested", "طلب معاينة"),
    "viewing_booked": bi("Viewing scheduled", "معاينة محجوزة"),
    "handoff_requested": bi("Human requested", "طلب موظف"),
    "contacted": bi("Contacted", "تم التواصل"),
    "negotiation": bi("Negotiation", "تفاوض"),
    "won": bi("Won", "تم التحويل"),
    "lost": bi("Lost", "خسارة"),
    "closed": bi("Closed", "مغلق"),
}

# The only statuses sales staff may set by hand. Everything else is driven by
# the assistant or by bookings (viewing_booked follows the bookings table).
STAFF_STATUSES = ("contacted", "qualified", "negotiation", "won", "lost")
CLOSED_STATUSES = ("won", "lost", "closed")

PRIORITY = {
    "low": bi("Low", "منخفضة"),
    "normal": bi("Normal", "عادية"),
    "high": bi("High", "عالية"),
    "urgent": bi("Urgent", "عاجلة"),
}

TASK_TYPE = {
    "call_customer": bi("Call the customer", "الاتصال بالعميل"),
    "send_details": bi("Send property details", "إرسال تفاصيل العقار"),
    "follow_up_property": bi("Follow up on a recommendation", "متابعة ترشيح عقار"),
    "confirm_viewing": bi("Confirm a viewing", "تأكيد معاينة"),
    "reengage_inactive": bi("Contact an inactive lead", "التواصل مع عميل متوقف"),
    "follow_up": bi("Scheduled follow-up", "متابعة مجدولة"),
    "other": bi("Other", "أخرى"),
}

TASK_STATUS = {
    "open": bi("Open", "مفتوحة"),
    "in_progress": bi("In progress", "قيد التنفيذ"),
    "done": bi("Done", "تمت"),
    "cancelled": bi("Cancelled", "ملغاة"),
}
ACTIVE_TASK_STATUSES = ("open", "in_progress")

BOOKING_STATUS = {
    "pending_confirmation": bi("Pending confirmation", "في انتظار التأكيد"),
    "confirmed": bi("Confirmed", "مؤكدة"),
    "cancelled": bi("Cancelled", "ملغاة"),
    "expired": bi("Expired", "منتهية"),
    "completed": bi("Completed", "تمت"),
}
ACTIVE_BOOKING_STATUSES = ("pending_confirmation", "confirmed")

DUPLICATE_DECISION = {
    "": bi("Pending review", "قيد المراجعة"),
    "confirmed": bi("Same person", "نفس الشخص"),
    "rejected": bi("Not the same person", "مش نفس الشخص"),
}

BAND = {
    "hot": bi("Hot", "ساخن"),
    "warm": bi("Warm", "دافئ"),
    "cold": bi("Cold", "بارد"),
}

TRANSACTION = {
    "sale": bi("Buy", "شراء"),
    "rent": bi("Rent", "إيجار"),
}
_TRANSACTION_ALIASES = {"buy": "sale", "purchase": "sale", "sale": "sale", "rent": "rent", "rental": "rent"}

EVIDENCE = {
    "same_phone_verified_both": bi("Same phone, verified on both", "نفس الرقم وموثّق على الاتنين"),
    "same_phone": bi("Same phone", "نفس رقم الهاتف"),
    "same_email": bi("Same email", "نفس الإيميل"),
}

# Phone-verification methods that prove the person controls the number. The
# website read-back ("readback") only proves the visitor typed it twice.
STRONG_VERIFICATION_METHODS = ("whatsapp_sender", "otp")


def label(vocabulary: dict[str, str], key: str | None) -> str:
    """Bilingual label for a stored key; unknown keys are shown as they are."""
    if key is None:
        return ""
    return vocabulary.get(key, key)


def parse_choice(vocabulary: dict[str, str], value: object) -> str | None:
    """Stored key for a cell value (a label or a bare key), or None if unknown."""
    text = " ".join(str(value or "").split())
    if not text:
        return "" if "" in vocabulary else None
    lowered = text.casefold()
    for key, shown in vocabulary.items():
        if lowered == shown.casefold() or (key and lowered == key.casefold()):
            return key
    return None


def normalize_transaction(value: object) -> str | None:
    return _TRANSACTION_ALIASES.get(str(value or "").strip().casefold())


# ---------------------------------------------------------------------------
# Lead-status rules for manual changes made through the workbook
# ---------------------------------------------------------------------------


def can_set_status(current: str, target: str) -> bool:
    """May sales staff move a lead from ``current`` to ``target``?

    Same status is a no-op and always fine. A lost lead can be reopened as
    ``contacted``; a won or closed lead is final.
    """
    if target == current:
        return True
    if target not in STAFF_STATUSES:
        return False
    if current == "lost":
        return target == "contacted"
    if current in CLOSED_STATUSES:
        return False
    if target in ("contacted", "qualified", "lost"):
        return True
    if target == "negotiation":
        return current in {"contacted", "qualified", "viewing_booked", "hot"}
    if target == "won":
        return current in {"contacted", "qualified", "viewing_booked", "negotiation"}
    return False


# ---------------------------------------------------------------------------
# Columns
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Column:
    key: str
    en: str
    ar: str
    width: int = 16
    kind: str = "text"  # text | date | datetime | int | money
    editable: bool = False
    hidden: bool = False
    choices: str | None = None  # name of the vocabulary behind the dropdown
    wrap: bool = False

    @property
    def header(self) -> str:
        return bi(self.en, self.ar)


LEAD_COLUMNS = (
    Column("lead_ref", "Lead ID", "رقم العميل", 11),
    Column("lead_uuid", "Lead UUID", "المعرّف الداخلي", 20, hidden=True),
    Column("name", "Customer name", "الاسم", 22),
    Column("phone", "Phone", "الهاتف", 16),
    Column("status", "Lead status", "الحالة", 22, editable=True, choices="status"),
    Column("assigned_agent", "Assigned agent", "المسؤول", 18, editable=True, choices="agents"),
    Column("next_followup", "Next follow-up", "المتابعة القادمة", 16, kind="date", editable=True),
    Column("last_contact", "Last contact", "آخر تواصل", 16, kind="date", editable=True),
    Column("notes", "Notes", "ملاحظات", 50, editable=True, wrap=True),
    Column("next_task", "Next task", "المهمة القادمة", 34),
    Column("score", "Score", "التقييم", 9, kind="int"),
    Column("band", "Priority band", "الفئة", 14),
    Column("qualified", "Qualified", "مؤهل", 12),
    Column("phone_verified", "Phone verified", "الهاتف موثّق", 14),
    Column("email", "Email", "الإيميل", 24),
    Column("channel", "Channel", "القناة", 14),
    Column("created", "Created", "تاريخ الإنشاء", 14, kind="date"),
    Column("language", "Language", "اللغة", 10),
    Column("transaction", "Transaction", "نوع المعاملة", 14),
    Column("location", "Location", "الموقع", 20),
    Column("compound", "Compound", "الكمبوند", 20),
    Column("property_type", "Property type", "نوع العقار", 16),
    Column("budget_min", "Budget min", "الميزانية من", 14, kind="money"),
    Column("budget_max", "Budget max", "الميزانية إلى", 14, kind="money"),
    Column("bedrooms", "Bedrooms", "الغرف", 10, kind="int"),
    Column("requirements", "Requirements", "المتطلبات", 40, wrap=True),
    Column("property_ref", "Property ref", "العقار المهتم به", 16),
    Column("last_message", "Last customer message", "آخر رسالة من العميل", 20, kind="datetime"),
    Column("next_viewing", "Next viewing", "المعاينة القادمة", 20, kind="datetime"),
    Column("open_handoff", "Human requested", "طلب موظف", 14),
    Column("person", "Person", "الشخص", 11),
    Column("possible_duplicate", "Possible duplicate", "تكرار محتمل", 14),
)

TASK_COLUMNS = (
    Column("task_ref", "Task ID", "رقم المهمة", 11),
    Column("task_uuid", "Task UUID", "المعرّف الداخلي", 20, hidden=True),
    Column("lead_ref", "Lead ID", "رقم العميل", 11),
    Column("customer", "Customer", "العميل", 22),
    Column("phone", "Phone", "الهاتف", 16),
    Column("task_type", "Task type", "نوع المهمة", 28, editable=True, choices="task_type"),
    Column("description", "Description", "الوصف", 44, editable=True, wrap=True),
    Column("assigned_agent", "Assigned agent", "المسؤول", 18, editable=True, choices="agents"),
    Column("due", "Due date", "تاريخ الاستحقاق", 15, kind="date", editable=True),
    Column("priority", "Priority", "الأولوية", 14, editable=True, choices="priority"),
    Column("status", "Status", "الحالة", 16, editable=True, choices="task_status"),
    Column("completed", "Completed", "تاريخ الإنجاز", 15, kind="date"),
    Column("source", "Source", "المصدر", 10),
    Column("created", "Created", "تاريخ الإنشاء", 15, kind="date"),
)

INTERACTION_COLUMNS = (
    Column("interaction_ref", "Interaction ID", "رقم التفاعل", 13),
    Column("lead_ref", "Lead ID", "رقم العميل", 11),
    Column("customer", "Customer", "العميل", 22),
    Column("when", "Date and time", "التاريخ والوقت", 19, kind="datetime"),
    Column("channel", "Channel", "القناة", 12),
    Column("modality", "Type", "النوع", 12),
    Column("customer_message", "Customer message", "رسالة العميل", 52, wrap=True),
    Column("assistant_response", "Assistant response", "رد المساعد", 52, wrap=True),
)

VIEWING_COLUMNS = (
    Column("booking_ref", "Booking ID", "رقم الحجز", 11),
    Column("lead_ref", "Lead ID", "رقم العميل", 11),
    Column("customer", "Customer", "العميل", 22),
    Column("phone", "Phone", "الهاتف", 16),
    Column("property_id", "Property", "العقار", 16),
    Column("starts", "Starts", "البداية", 19, kind="datetime"),
    Column("ends", "Ends", "النهاية", 19, kind="datetime"),
    Column("status", "Status", "الحالة", 24),
    Column("agent", "Agent", "الموظف", 16),
    Column("notes", "Notes", "ملاحظات", 40, wrap=True),
)

DUPLICATE_COLUMNS = (
    Column("pair_key", "Pair key", "مفتاح الزوج", 14, hidden=True),
    Column("a_name", "Customer A", "العميل (أ)", 22),
    Column("a_phone", "Phone A", "هاتف (أ)", 16),
    Column("a_channel", "Channel A", "قناة (أ)", 12),
    Column("a_verified", "Verified A", "موثّق (أ)", 12),
    Column("b_name", "Customer B", "العميل (ب)", 22),
    Column("b_phone", "Phone B", "هاتف (ب)", 16),
    Column("b_channel", "Channel B", "قناة (ب)", 12),
    Column("b_verified", "Verified B", "موثّق (ب)", 12),
    Column("evidence", "Evidence", "السبب", 30),
    Column("decision", "Decision", "القرار", 24, editable=True, choices="dup_decision"),
    Column("reviewed_by", "Reviewed by", "راجعه", 16, editable=True, choices="agents"),
    Column("reviewed_at", "Reviewed at", "تاريخ المراجعة", 17, kind="datetime"),
)

COLUMNS_BY_SHEET = {
    LEADS: LEAD_COLUMNS,
    TASKS: TASK_COLUMNS,
    INTERACTIONS: INTERACTION_COLUMNS,
    VIEWINGS: VIEWING_COLUMNS,
    DUPLICATES: DUPLICATE_COLUMNS,
}


def _check_unique_headers() -> None:
    for sheet, columns in COLUMNS_BY_SHEET.items():
        headers = [column.header for column in columns]
        keys = [column.key for column in columns]
        assert len(set(headers)) == len(headers), f"duplicate header on {sheet}"
        assert len(set(keys)) == len(keys), f"duplicate key on {sheet}"


_check_unique_headers()


def column_index(sheet: str, key: str) -> int:
    """1-based column number of ``key`` on ``sheet``."""
    for position, column in enumerate(COLUMNS_BY_SHEET[sheet], start=1):
        if column.key == key:
            return position
    raise KeyError(f"{sheet} has no column {key!r}")
