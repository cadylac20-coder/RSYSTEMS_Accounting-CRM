"""
Database layer for the Accounting CRM.

Production (Render): set TURSO_DATABASE_URL + TURSO_AUTH_TOKEN and every
get_db() call talks to your Turso database over plain HTTP via the
`libsql-client` package — pure Python, no native/Rust compilation, so it
builds reliably on Render (unlike libsql-experimental, which requires a
Rust toolchain and fails on Render's read-only build filesystem).

Local dev: leave those env vars unset and it transparently falls back to a
local SQLite file — same code path, same SQL, nothing to change.
"""
import os
import sqlite3

USE_TURSO = bool(os.environ.get("TURSO_DATABASE_URL"))
LOCAL_DB_PATH = os.environ.get("LOCAL_DB_PATH", "accounting_crm.db")


class Row(dict):
    """Dict-like row — lets existing `dict(row)` / `row['col']` call sites
    work identically whether the underlying driver is sqlite3 or Turso."""
    pass


class RowCursor:
    """Normalizes both sqlite3 cursor results and libsql-client ResultSets
    into the same fetchone()/fetchall() dict-row interface, so main.py
    never has to know which driver actually ran the query."""
    def __init__(self, columns, rows, lastrowid=None):
        self._columns = columns
        self._rows = rows
        self._pos = 0
        self.lastrowid = lastrowid

    def _to_row(self, raw):
        if raw is None:
            return None
        return Row(zip(self._columns, raw))

    def fetchone(self):
        if self._pos >= len(self._rows):
            return None
        raw = self._rows[self._pos]
        self._pos += 1
        return self._to_row(raw)

    def fetchall(self):
        remaining = self._rows[self._pos:]
        self._pos = len(self._rows)
        return [self._to_row(r) for r in remaining]


class DBConn:
    """Thin wrapper so main.py never has to know whether it's talking to
    Turso or local SQLite — same .execute()/.commit()/.close() surface."""
    def __init__(self, driver, raw):
        self._driver = driver  # "sqlite" | "turso"
        self._raw = raw

    def execute(self, sql, params=()):
        params = list(params) if params else []

        if self._driver == "sqlite":
            cur = self._raw.execute(sql, params)
            columns = [d[0] for d in cur.description] if cur.description else []
            rows = cur.fetchall()
            return RowCursor(columns, rows, cur.lastrowid)

        # Turso, via libsql-client's HTTP/Hrana transport.
        rs = self._raw.execute(sql, params)
        columns = list(rs.columns) if rs.columns else []
        rows = [tuple(r) for r in rs.rows]
        lastrowid = None
        if sql.strip()[:6].upper() == "INSERT":
            try:
                id_rs = self._raw.execute("SELECT last_insert_rowid()")
                lastrowid = id_rs.rows[0][0]
            except Exception:
                lastrowid = None
        return RowCursor(columns, rows, lastrowid)

    def executescript(self, sql):
        # Neither driver needs a special multi-statement mode here — just
        # run each CREATE TABLE statement one at a time. This only runs
        # once at startup (init_db()), so the extra round-trips on Turso
        # don't matter in practice.
        for stmt in [s.strip() for s in sql.split(";") if s.strip()]:
            self.execute(stmt)

    def commit(self):
        if self._driver == "sqlite":
            self._raw.commit()
        # libsql-client applies each statement immediately over HTTP —
        # there's no local transaction buffer to flush, so this is a no-op
        # on the Turso path.

    def close(self):
        try:
            self._raw.close()
        except Exception:
            pass


def get_db() -> DBConn:
    if USE_TURSO:
        import libsql_client
        try:
            client = libsql_client.create_client_sync(
                url=os.environ["TURSO_DATABASE_URL"],
                auth_token=os.environ.get("TURSO_AUTH_TOKEN"),
            )
        except Exception as e:
            raise RuntimeError(
                f"Could not connect to Turso ({e}). Check TURSO_DATABASE_URL "
                f"and TURSO_AUTH_TOKEN are set correctly on Render."
            ) from e
        return DBConn("turso", client)
    else:
        conn = sqlite3.connect(LOCAL_DB_PATH)
        return DBConn("sqlite", conn)


SCHEMA = """
CREATE TABLE IF NOT EXISTS admin_users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'staff',
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS clients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    entity_type TEXT NOT NULL DEFAULT 'individual',
    pan TEXT,
    gstin TEXT,
    tan TEXT,
    email TEXT,
    phone TEXT,
    address TEXT,
    services_json TEXT NOT NULL DEFAULT '[]',
    assigned_to INTEGER,
    status TEXT NOT NULL DEFAULT 'active',
    notes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    company TEXT,
    phone TEXT,
    email TEXT,
    service_interested TEXT,
    source TEXT NOT NULL DEFAULT 'other',
    status TEXT NOT NULL DEFAULT 'new',
    assigned_to INTEGER,
    notes TEXT,
    converted_client_id INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS compliance_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL,
    task_type TEXT NOT NULL,
    period_label TEXT NOT NULL,
    due_date TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    assigned_to INTEGER,
    filed_date TEXT,
    documents_received INTEGER NOT NULL DEFAULT 0,
    notes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS compliance_templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_type TEXT NOT NULL,
    frequency TEXT NOT NULL DEFAULT 'monthly',
    due_day INTEGER NOT NULL DEFAULT 20,
    due_month_offset INTEGER NOT NULL DEFAULT 1,
    description TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS client_compliance_subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL,
    template_id INTEGER NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS checklists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    service_type TEXT NOT NULL,
    name TEXT NOT NULL,
    documents_json TEXT NOT NULL DEFAULT '[]',
    format_mode TEXT NOT NULL DEFAULT 'list',
    free_text TEXT DEFAULT '',
    created_by TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS invoices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_no TEXT NOT NULL UNIQUE,
    client_id INTEGER NOT NULL,
    items_json TEXT NOT NULL DEFAULT '[]',
    subtotal REAL NOT NULL DEFAULT 0,
    tax_amount REAL NOT NULL DEFAULT 0,
    total REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'draft',
    issue_date TEXT NOT NULL,
    due_date TEXT,
    paid_amount REAL NOT NULL DEFAULT 0,
    paid_date TEXT,
    notes TEXT,
    created_by TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ledger_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL,
    entry_type TEXT NOT NULL,
    amount REAL NOT NULL,
    ref_invoice_id INTEGER,
    description TEXT,
    entry_date TEXT NOT NULL,
    created_by TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL,
    notice_type TEXT NOT NULL,
    notice_ref_no TEXT,
    received_date TEXT NOT NULL,
    response_due_date TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    description TEXT,
    assigned_to INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS letter_templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    template_type TEXT NOT NULL,
    subject TEXT,
    body_template TEXT NOT NULL,
    created_by TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS calendar_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    description TEXT,
    event_type TEXT NOT NULL DEFAULT 'general',
    client_id INTEGER,
    start_at TEXT NOT NULL,
    reminder_minutes_before INTEGER,
    reminder_email TEXT,
    reminder_sent INTEGER NOT NULL DEFAULT 0,
    created_by TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS staff_activity_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    staff_id INTEGER,
    staff_name TEXT,
    staff_role TEXT,
    action TEXT,
    detail TEXT,
    session_id TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS team_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sender_id INTEGER,
    sender_name TEXT,
    sender_role TEXT,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def init_db():
    conn = get_db()
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
