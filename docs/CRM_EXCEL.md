# Excel CRM for the sales team

PostgreSQL stays the **only** source of truth. The workbook is a generated, signed snapshot the
sales team can sort, filter, report on and edit; edits come back only through a validated import.
Nothing here is imported by the assistant, the RAG pipeline, the channel handlers or the LangGraph
subgraphs (`conversational`, `property`, `qualification`, `sales`, `scheduling`): a problem in the
CRM workbook can never interrupt a conversation (`tests/unit/test_crm_excel_boundaries.py` enforces it).

```
customer ──► website / WhatsApp ──► assistant ──► PostgreSQL ◄── GET  /admin/crm/export.xlsx ──► crm-YYYYMMDD-HHMM.xlsx
 (unchanged)                       (unchanged)    customers, leads,        (built in memory,            │ sales edit
                                                  interactions, handoffs,  never written to disk)       ▼ in Excel
                                                  bookings, crm_tasks,  ◄── POST /admin/crm/import ◄── same file
                                                  crm_duplicate_reviews     (preview first, then apply)
```

## 1. Day-to-day use

1. **Download** (needs `X-Admin-Key`; use `CRM_API_KEY` for the sales team):

   ```powershell
   Invoke-WebRequest https://<your-api>/admin/crm/export.xlsx -Headers @{"X-Admin-Key"="<key>"} -OutFile crm.xlsx
   ```
2. **Edit the yellow columns only** and save as `.xlsx`. Sort, filter and reorder freely; do not rename or delete columns or sheets.
3. **Preview** (changes nothing; this is the default):

   ```powershell
   Invoke-RestMethod -Method Post -Uri https://<your-api>/admin/crm/import -InFile crm.xlsx -ContentType application/octet-stream -Headers @{"X-Admin-Key"="<key>"}
   ```
4. **Apply**: the same call with `?dry_run=false` (add `&all_or_nothing=true` to apply nothing if any row has a problem).
5. Read the JSON report: each row is `applied`, `partial`, `conflict`, `error` or `duplicate`, with the exact reason.
   A **conflict** means someone (often the assistant) changed that field after your download; the database value is kept.
   Download again and redo that one edit.

curl: `curl --data-binary @crm.xlsx -H "X-Admin-Key: <key>" -H "Content-Type: application/octet-stream" "https://<your-api>/admin/crm/import?dry_run=false"`

The file is a **snapshot**: it does not update by itself. Download again for fresh data (the Dashboard formulas do recalculate
as you edit Leads and Tasks, and "due today / overdue" follow the clock when you open the file).

## 2. Workbook

| Sheet | What it is | Editable |
|---|---|---|
| Guide | Rules, snapshot time, anything left out by the size caps | - |
| Dashboard | KPIs (total/new/qualified/unqualified, by channel/type/location/status, due today, overdue, viewings, conversion) and charts | - |
| Leads | One row per lead (Excel Table) | status, assigned agent, next follow-up, last contact, notes |
| Interactions | Last 90 days, one row per turn, messages cut to 300 characters (the full conversation stays in PostgreSQL) | - |
| Tasks | Follow-ups (Excel Table); add a row to create one | type, description, agent, due date, priority, status |
| Viewings | Bookings with their lead | - |
| Duplicates | Possible duplicate customers | decision, reviewed by |
| Lists | Dropdown sources (agents from `CRM_AGENTS`) | - |

Headers are bilingual (English | Arabic), dates are shown in `VIEWING_TIMEZONE` (default `Africa/Cairo`), phone numbers are text.
Overdue follow-ups are red, due today amber. Customer text is always stored as text, so a message starting with `=` can never run as a formula.

## 3. Lead lifecycle

The assistant moves a lead forward automatically; the sales team takes over from there.

`new → qualifying → qualified → contact_requested / viewing_requested → viewing_booked → handoff_requested → contacted → negotiation → won / lost`

| Who | Can set | Rules |
|---|---|---|
| Assistant / bookings | new, qualifying, hot, contact_requested, viewing_requested, viewing_booked, handoff_requested | Only ever moves forward; never touches `negotiation`, `won`, `lost`, `closed` |
| Sales (Excel) | contacted, qualified, negotiation, won, lost | `contacted`/`qualified`/`lost` from any open stage; `negotiation` from contacted, qualified, viewing_booked, hot; `won` from contacted, qualified, viewing_booked, negotiation; `lost` can be reopened as `contacted`; `won` is final |

Side effects: setting `contacted`/`negotiation` with an empty Last contact fills today; `won`/`lost` cancels the lead's automatic tasks;
a Next follow-up date becomes one `follow_up` task (moved, cleared or reopened, never duplicated). A returning customer of a `won`/`lost`
lead starts a **new** lead. Scoring is the existing one (`app/leads/scoring.py`: hot ≥ 70, warm ≥ 40); the workbook never recomputes it.

## 4. Tasks created automatically (before each export, idempotent)

| Situation | Task | Priority | Closes when |
|---|---|---|---|
| Customer asked for a person (open/claimed handoff) | Call the customer | high | handoff resolved |
| Viewing request waiting for confirmation | Confirm a viewing | high | booking confirmed / cancelled / expired |
| Hot lead (score ≥ 70), nobody has contacted it | Contact the customer | high | lead contacted / negotiation / closed |
| No customer message and no contact for `CRM_INACTIVE_DAYS` (7) | Contact an inactive lead | normal | customer writes back / lead closed |

Each task has a unique `dedupe_key`, so repeated or simultaneous exports create it once; a task someone cancelled is never recreated.
Staff tasks are never closed automatically.

## 5. Identity and duplicates

Customers are separate per channel **on purpose** (merging on an unverified number would expose one person's data to another).
A number is *verified* only when the person demonstrably controls it: the WhatsApp sender id (and, in future, an OTP). A number typed on
the website is not. So the same phone on two channels is **flagged** on Leads and listed on Duplicates; only a pair verified on both sides
is grouped automatically. Staff choose *Same person* / *Not the same person*; the choice groups the rows under one `P-xxxxxx` Person key
for reporting. It never merges or changes customer records, and the assistant never sees it.

## 6. Deploy

1. `requirements.txt` must contain `openpyxl` and `defusedxml` (`apply_crm_edits.py` adds them; `tests/unit/test_crm_excel_boundaries.py` fails if they are missing).
   They are imported only when a CRM route is called: if they were ever missing, `/admin/crm/*` answers `503` and everything else keeps running.
2. Environment (Render → Environment):

   | Variable | Default | Meaning |
   |---|---|---|
   | `ADMIN_API_KEY` | - | Existing operator key. Also works on `/admin/crm/*`; the workbook signature derives from it |
   | `CRM_API_KEY` | unset | Give this to the sales team. **≥ 24 random characters**; opens `/admin/crm/*` only |
   | `CRM_SIGNING_KEY` | unset | Optional dedicated signing key (so rotating `ADMIN_API_KEY` does not invalidate downloaded files) |
   | `CRM_AGENTS` | empty | `Mona, Ahmed, Sara`: the agent dropdown. Empty = any name accepted |
   | `CRM_EXPORT_MAX_LEADS` / `CRM_EXPORT_MAX_INTERACTIONS` | 5000 / 10000 | Memory guard (see §8) |
   | `CRM_IMPORT_MAX_MB` / `CRM_IMPORT_MAX_ROWS` | 10 / 10000 | Upload limits |
   | `CRM_INACTIVE_DAYS` | 7 | Quiet period before a re-engage task (0 disables) |
3. **Migrate before the new code takes traffic** (`python scripts/migrate.py`; revision `b3d5f1a8c2e7`). It is additive and nullable, so the old code keeps working on the migrated database.
   The readiness check reports the database as outdated until it has run.
4. Deploy, then smoke test: download the workbook, open it in Excel (no "repair" prompt), run an import preview with the unedited file (expect 0 changes).
5. Rotate any key that was ever shared in chat or a ticket.

## 7. Operations

* **Audit**: every download and import is in `admin_audit` (`crm.export`, `crm.import.preview`, `crm.import`) with counts and field-level changes; note text is never logged.
* **Logs**: `CRM import | dry_run=… applied=… {…}`; task-sync failures log `CRM task sync failed` and the export continues.
* **Backups**: PostgreSQL (Neon point-in-time restore / branches) is the backup. Downloaded workbooks are disposable snapshots, not backups.
* **Recovery**: restore the database, then download a fresh workbook. Old files import only against the deployment whose key signed them, and fields changed since are reported as conflicts rather than overwritten.
* **Key rotation**: changing `ADMIN_API_KEY` (or `CRM_SIGNING_KEY`) invalidates already-downloaded files, which are refused with "export a fresh file".
* **Rollback**: `alembic downgrade a7c1e0d2b9f4` drops the CRM tables and the three lead columns; lead, customer and conversation data are untouched.

| Failure | What happens |
|---|---|
| Database unreadable during export | That request fails (5xx); conversations and the rest of the API are unaffected; nothing is lost |
| Another export/import running | `429` + `Retry-After` (one at a time per process) |
| Task creation fails | Export still succeeds, without new tasks |
| Workbook packages missing | `/admin/crm/*` → `503`; everything else normal |
| Corrupt, truncated, macro-enabled, foreign or altered upload | `400` with a clear reason; nothing written |
| A row cannot be saved | That row is reported as an error; the others apply (`all_or_nothing` undoes all) |

## 8. Limits and what to do about them

* **Not real-time.** It is a download/upload cycle by design; Excel is never the database.
* **Memory.** The file is built in memory: ~90 MB for 5,000 leads + 10,000 interactions (~3 s); 20,000 + 50,000 reaches ~530 MB, more than a 512 MB host has.
  Raise the caps only with the instance size; older rows are left out and the Guide sheet says how many.
* **No per-user identity.** One shared key means the audit log shows the client IP and the action, not which salesperson. Per-user accounts (JWT/SSO) are the next step if that matters.
* **Personal data.** The workbook contains names and phone numbers. Share it only inside the team, delete old copies, and review obligations under Egypt's personal-data protection law with your legal adviser.
* **One process.** The single-flight gate is per process; database row locks keep imports correct across instances.
* **Bookings have no `lead_id`** in the current booking flow, so the workbook links each booking to its lead when reading (appointment id, else the customer's lead at that time).
  Setting `lead_id` when a booking is created would make this exact; it was left alone to avoid touching the booking flow.
* **"The assistant could not answer"** is not recorded anywhere today, so it cannot create a task. A single call from the `conversational` / `property` fallback path could add it later.

Scaling path, in order: raise the instance memory and caps → move generation to a background job writing to object storage (S3/R2) and return a link →
per-user authentication → website phone OTP (lets website customers be verified and linked automatically) → push the same tables to a hosted CRM (Leads, Interactions, Tasks map one-to-one).

## 9. Tests

| Scenario | Test |
|---|---|
| 1 new website customer · 2 new WhatsApp customer · 3 returns, same channel · 4 other channel · 5 gradual requirements · 7 assistant + salesperson · 8 broken/locked/missing · 9 asks for a human · 10 status & follow-up edits | `tests/integration/test_crm_scenarios.py` |
| 6 many customers at once · 7 simultaneous update (real threads, PostgreSQL) | `tests/integration/test_crm_scenarios_postgres.py` |
| Workbook layout, safety, signing, identity rules, dashboard maths (recalculated in LibreOffice when installed) | `tests/unit/test_crm_excel_workbook.py` |
| Upload safety, per-field merge, validation | `tests/unit/test_crm_excel_importer_core.py` |
| Separation from the assistant, lazy loading, declared dependencies | `tests/unit/test_crm_excel_boundaries.py` |
| Database projection, task sync, export and import APIs | `test_crm_excel_queries.py`, `test_crm_tasks_sync.py`, `test_crm_export_api.py`, `test_crm_import.py`, `test_crm_import_api.py` |

```powershell
pytest tests/unit tests/integration -k "crm or admin"      # everything above on SQLite
pytest -m postgres                                          # the concurrency tests (needs TEST_DATABASE_URL)
python scripts/make_sample_crm_workbook.py                  # a sample file to open in Excel
```

## 10. Code map

New: `app/crm/excel/` (`schema`, `text`, `baseline`, `signing`, `identity`, `data`, `errors`, `meta`, `exporter`, `queries`, `tasks`, `service`, `importer_core`, `importer`),
`app/api/routes/crm_admin.py`, migration `b3d5f1a8c2e7`. Existing files touched (small, additive): `database/models.py` (3 lead columns, 2 tables),
`agents/tools/crm.py` (`negotiation` rank; a won/lost lead is final for automatic status updates), `helpers/config.py` (CRM settings),
`api/routes/__init__.py` and `main.py` (register the router), `requirements.txt`.
