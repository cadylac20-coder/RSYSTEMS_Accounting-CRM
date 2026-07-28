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
2. On Render, create a Web Service from this repo:
   - Build command: `pip install -r requirements.txt`
   - Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
3. Set these environment variables on Render:
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

## Notes / things worth knowing

- **`runtime.txt` pins Python to 3.11.9.** Render currently defaults new
  services to Python 3.14, which is too new for several dependencies here
  (notably `pydantic-core`) to have prebuilt wheels yet — without a pin,
  pip falls back to compiling them from Rust source, which fails on
  Render's read-only build filesystem (the same class of error as the
  `libsql-experimental` issue below). If Render ignores `runtime.txt` for
  your account, set the `PYTHON_VERSION` environment variable to `3.11.9`
  on the service instead — check whichever mechanism Render's current docs
  recommend at deploy time, since this has changed before.
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
  few writes against your real Turso DB after deploying, in case that
  package's exact API has shifted since this was written.
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
