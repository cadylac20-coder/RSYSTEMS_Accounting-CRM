# Rsystems Accounting CRM

FastAPI backend + single-page `admin.html` frontend, built to match the
structure and conventions of the Visa CRM: same 3-tier role model
(staff / admin / superadmin), same IST-everywhere timestamp handling, same
mobile drawer navigation, same numbered-list/free-form checklist export.

## What's included

- **Clients** — entity type, PAN/GSTIN/TAN, subscribed services, assignment
- **Leads** — pipeline with admin-only assignment (mirrors the Visa CRM's
  restricted lead-assignment pattern)
- **Compliance Tasks** — GST/TDS/ITR/ROC filings per client per period, with
  automatic overdue detection
- **Recurring Filings** — define a filing type once (e.g. "GSTR-3B, monthly,
  due 20 days after period end"), subscribe clients to it, and generate each
  period's tasks with one click — accounting-specific, no Visa CRM equivalent
- **Notices** — government/department notices per client and response
  deadlines — also accounting-specific
- **Invoices & Ledger** — line-item invoices, tax %, payment recording,
  running client balance, PDF export
- **Document Checklists** — numbered list or free-form text, PDF export,
  can bundle in letter templates on export
- **Letter Templates** — engagement letters, fee reminders, etc.
- **Calendar** — month grid, upcoming-deadlines widget on the dashboard
- **Staff Monitor** — online/last-seen + activity log
- **Staff & Roles**, **Team Chat**, **Export CRM** (CSV)

## Local setup (no Turso needed)

```bash
pip install -r requirements.txt
uvicorn main:app --reload
```

This uses a local `accounting_crm.db` SQLite file automatically — no env
vars required. Open http://localhost:8000.

On first run it seeds one superadmin account so you're never locked out:
- email: `admin@rsystems.in` (override with `SEED_ADMIN_EMAIL`)
- password: `ChangeMe123!` (override with `SEED_ADMIN_PASSWORD`)

**Change that password immediately after your first login**, via Staff & Roles.

## Deploying on Render + Turso

1. Create a Turso database (`turso db create rsystems-accounting`) and grab:
   - the database URL (`turso db show rsystems-accounting --url`)
   - an auth token (`turso db tokens create rsystems-accounting`)
2. On Render, create a **Blueprint** from this repo (New → Blueprint, point it
   at this repo) rather than a plain Web Service — `render.yaml` at the repo
   root now defines the service, build/start commands, and Python version
   declaratively, so Render picks all of that up automatically instead of
   depending on a `runtime.txt` file (see "Why `render.yaml` instead of
   `runtime.txt`" below for why that changed).
   - If you'd rather not use a Blueprint, a plain Web Service works too —
     just set the Build/Start commands manually (below) and add
     `PYTHON_VERSION=3.11.9` yourself under Environment.
   - Build command: `pip install -r requirements.txt`
   - Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
3. Set these environment variables on Render (the Blueprint will prompt you
   for the ones marked `sync: false` in `render.yaml` when you deploy it —
   you still need to type the actual values in, they're just not committed
   to the repo):
   | Variable | Value |
   |---|---|
   | `TURSO_DATABASE_URL` | your Turso database URL (`libsql://...`) |
   | `TURSO_AUTH_TOKEN` | your Turso auth token |
   | `JWT_SECRET` | any long random string |
   | `SEED_ADMIN_EMAIL` | your real admin email (optional) |
   | `SEED_ADMIN_PASSWORD` | a strong password (optional but recommended) |

That's it — `database.py` detects `TURSO_DATABASE_URL` and switches from the
local SQLite fallback to Turso automatically; no code changes needed between
local dev and production.

## Why `render.yaml` instead of `runtime.txt`

This repo no longer ships a `runtime.txt`. Two real problems with it:

1. **Render doesn't reliably honor it.** Render's own troubleshooting docs
   list "setting the Python version doesn't change your service's Python
   version" as a known issue, and Render's community forum has multiple
   threads of `runtime.txt` being silently ignored on services created after
   a certain point — the service just keeps using whatever Python version it
   defaulted to when it was first created, pin file or not.
2. **It matters here more than usual.** `pydantic` (a FastAPI dependency)
   compiles a Rust extension (`pydantic-core`) at install time when there's
   no prebuilt wheel for the Python version in use. As of this writing,
   `pydantic-core` does not yet ship a standard Linux wheel for Python 3.14 —
   only an experimental Pyodide/WebAssembly build — so if Render runs this
   service on 3.14 (its current default for new services), `pip install`
   falls back to compiling Rust from source, which fails outright on
   Render's read-only build filesystem. **This is almost certainly the exact
   error you were hitting.**

`render.yaml` is Render's current declarative mechanism for exactly this —
it sets `PYTHON_VERSION` as part of the service definition itself, read at
service-creation time, rather than as a file Render's build step may or may
not check. **Important caveat:** if you already have an existing Render
service for this repo, Python version is fixed at the time a service was
*first created* — redeploying with `render.yaml` present may not retroactively
change an existing service's Python version. If your Render error persists
after adding `render.yaml`, delete the existing service and recreate it as a
fresh Blueprint deploy rather than pushing to the old one.

## Notes / things worth knowing

- **This uses `libsql-client`, not `libsql-experimental`.** An earlier
  version of this project used `libsql-experimental`, which requires
  compiling a Rust extension at install time — that fails on Render because
  its build environment gives you a read-only Cargo cache directory. If you
  ever see a build error mentioning `maturin`, `cargo metadata`, or
  `Read-only file system`, that's this problem; `libsql-client` avoids it
  entirely by talking to Turso over plain HTTP instead of an embedded
  native library, so there's nothing to compile.
- I wasn't able to run this against a live Turso database from this sandbox
  (no outbound network access here) — please smoke-test signup/login and a
  few writes against your real Turso DB after deploying.
- **Recurring filing due-dates now clamp to the real length of the due
  month**, not a blanket "28th." A template with `due_day=31` (e.g. an ITR
  deadline on the 31st) previously always landed on the 28th, even in
  31-day months — that's fixed (`_clamp_day()` in `main.py` uses
  `calendar.monthrange()` to only clamp when the month is actually shorter
  than the configured due day, e.g. February).
- **Checklist/letter-template/invoice exports now preserve exact spacing.**
  Free-form checklist text, letter template bodies, and numbered checklist
  items are now run through a formatting-preserving renderer before going
  into the PDF, so manually typed indentation and multi-space alignment
  survive export instead of being collapsed — same fix applied to the Visa
  CRM's checklist export, applied here too since this app uses the same
  reportlab-based export pattern. All user-entered text (service names,
  checklist names, item text) is also now XML-escaped before being placed in
  a PDF `Table` cell, not just in `Paragraph` text — an unescaped `&` or `<`
  in a service name could otherwise silently break that cell's rendering.
- **Recurring filing generation is on-demand, not scheduled.** Click
  "Generate Due Tasks Now" on the Recurring Filings page, or wire a Render
  Cron Job to hit `POST /admin/compliance/generate-recurring` monthly
  (needs an admin/superadmin bearer token).
- **Reminders aren't emailed yet.** Calendar events store a reminder time
  and email, but nothing currently sends that email — the Visa CRM's
  `/admin/calendar/due-reminders` polling pattern would need to be added
  here too if you want that.
- Change `JWT_SECRET` and the seed admin password before going live —
  the defaults are meant for local dev only.
