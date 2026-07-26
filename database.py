"""
Database layer for the Accounting CRM.

Production (Render): set TURSO_DATABASE_URL + TURSO_AUTH_TOKEN and every
get_db() call talks to your Turso (libSQL) database via an embedded replica.
Local dev: leave those unset and it transparently falls back to a local
SQLite file — same code path, same SQL, nothing to change.
"""
import os
import sqlite3

USE_TURSO = bool(os.environ.get("TURSO_DATABASE_URL"))
LOCAL_DB_PATH = os.environ.get("LOCAL_DB_PATH", "accounting_crm.db")
LOCAL_REPLICA_PATH = os.environ.get("LOCAL_REPLICA_PATH", "turso-replica.db")


class Row(dict):
    """Dict-like row — lets existing `dict(row)` / `row['col']` call sites
    work identically whether the underlying driver is sqlite3 or libsql."""
    pass


class RowCursor:
    def __init__(self, cursor):
        self._cursor = cursor
        self.lastrowid = getattr(cursor, "lastrowid", None)

    def _to_row(self, raw):
        if raw is None:
            return None
        cols = [d[0] for d in self._cursor.description]
        return Row(zip(cols, raw))

    def fetchone(self):
        return self._to_row(self._cursor.fetchone())

    def fetchall(self):
        return [self._to_row(r) for r in self._cursor.fetchall()]


class DBConn:
    """Thin wrapper so main.py never has to know whether it's talking to
    Turso or local SQLite — same .execute()/.commit()/.close() surface."""
    def __init__(self, raw_conn):
        self._conn = raw_conn

    def execute(self, sql, params=()):
        cur = self._conn.execute(sql, params)
        return RowCursor(cur)

    def executescript(self, sql):
        # libsql-experimental doesn't support executescript(); split and
        # run statements individually so schema init works on both drivers.
        for stmt in [s.strip() for s in sql.split(";") if s.strip()]:
            self._conn.execute(stmt)

    def commit(self):
        self._conn.commit()
        if USE_TURSO:
            try:
                self._conn.sync()
            except Exception:
                pass  # a failed background sync shouldn't crash a request

    def close(self):
        try:
            self._conn.close()
        except Exception:
            pass


def get_db() -> DBConn:
    if USE_TURSO:
        import libsql_experimental as libsql
        conn = libsql.connect(
            LOCAL_REPLICA_PATH,
            sync_url=os.environ["TURSO_DATABASE_URL"],
            auth_token=os.environ["TURSO_AUTH_TOKEN"],
        )
        try:
            conn.sync()
        except Exception:
            pass
    else:
        conn = sqlite3.connect(LOCAL_DB_PATH)
    return DBConn(conn)


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
