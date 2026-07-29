"""
Database layer for the Accounting CRM.

Production (Render): set TURSO_DATABASE_URL + TURSO_AUTH_TOKEN and every
get_db() call talks to your Turso database over Turso's documented HTTP
"pipeline" API (https://docs.turso.tech/sdk/http/quickstart) using plain
`requests` calls — pure Python, no native/Rust compilation, so it builds
reliably on Render, and no WebSocket handshake to fail either.

This previously used the `libsql-client` package, which talks to Turso over
a WebSocket (the Hrana protocol). That package is now archived upstream
(tursodatabase/libsql-client-py), and its WebSocket transport has multiple
open reports of failing against real Turso databases with exactly this
error:

    aiohttp.client_exceptions.WSServerHandshakeError: 400,
    message='Invalid response status'

— even though the exact same URL works fine via the Turso CLI or plain
HTTP. Talking to Turso's plain HTTP endpoint directly sidesteps that
transport entirely: it's the same API Turso's own docs recommend for
edge/serverless runtimes, and it's just JSON over HTTPS, so there's nothing
version- or protocol-sensitive about it.

Local dev: leave TURSO_DATABASE_URL/TURSO_AUTH_TOKEN unset and it
transparently falls back to a local SQLite file — same code path, same SQL,
nothing to change.
"""
import os
import json
import base64
import sqlite3

import requests

USE_TURSO = bool(os.environ.get("TURSO_DATABASE_URL"))
LOCAL_DB_PATH = os.environ.get("LOCAL_DB_PATH", "accounting_crm.db")


def _turso_http_base_url(raw_url: str) -> str:
    """Turso gives you a libsql://... URL (meant for the websocket-based
    protocol). The HTTP pipeline API just wants the same host over
    https:// — this also transparently handles someone pasting in an
    https:// URL directly, or a wss:///ws:// one."""
    if raw_url.startswith("libsql://"):
        return "https://" + raw_url[len("libsql://"):]
    if raw_url.startswith("wss://"):
        return "https://" + raw_url[len("wss://"):]
    if raw_url.startswith("ws://"):
        return "http://" + raw_url[len("ws://"):]
    return raw_url.rstrip("/")


def _to_turso_value(v):
    """Encode a Python value into Turso's typed JSON arg format."""
    if v is None:
        return {"type": "null"}
    if isinstance(v, bool):
        return {"type": "integer", "value": str(int(v))}
    if isinstance(v, int):
        return {"type": "integer", "value": str(v)}
    if isinstance(v, float):
        return {"type": "float", "value": v}
    if isinstance(v, (bytes, bytearray)):
        return {"type": "blob", "value": base64.b64encode(v).decode()}
    return {"type": "text", "value": str(v)}


def _from_turso_value(v):
    """Decode one of Turso's typed JSON row values back into a plain
    Python value."""
    if v is None:
        return None
    t = v.get("type")
    if t == "null":
        return None
    if t == "integer":
        return int(v["value"])
    if t == "float":
        return float(v["value"])
    if t == "blob":
        return base64.b64decode(v["value"])
    return v.get("value")


class Row(dict):
    """Dict-like row — lets existing `dict(row)` / `row['col']` call sites
    work identically whether the underlying driver is sqlite3 or Turso."""
    pass


class RowCursor:
    """Normalizes both sqlite3 cursor results and Turso's HTTP pipeline
    results into the same fetchone()/fetchall() dict-row interface, so
    main.py never has to know which driver actually ran the query."""
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
    Turso (over plain HTTP) or local SQLite — same .execute()/.commit()/
    .close() surface either way."""
    def __init__(self, driver, raw=None, http_base=None, http_token=None):
        self._driver = driver  # "sqlite" | "turso"
        self._raw = raw
        self._http_base = http_base
        self._http_token = http_token

    def execute(self, sql, params=()):
        params = list(params) if params else []

        if self._driver == "sqlite":
            cur = self._raw.execute(sql, params)
            columns = [d[0] for d in cur.description] if cur.description else []
            rows = cur.fetchall()
            return RowCursor(columns, rows, cur.lastrowid)

        # Turso, via a single HTTP POST to its /v2/pipeline endpoint —
        # see https://docs.turso.tech/sdk/http/reference
        payload = {
            "requests": [
                {"type": "execute", "stmt": {
                    "sql": sql,
                    "args": [_to_turso_value(p) for p in params],
                }},
                {"type": "close"},
            ]
        }
        try:
            resp = requests.post(
                f"{self._http_base}/v2/pipeline",
                headers={
                    "Authorization": f"Bearer {self._http_token}",
                    "Content-Type": "application/json",
                },
                data=json.dumps(payload),
                timeout=20,
            )
        except requests.RequestException as e:
            raise RuntimeError(f"Could not reach Turso at {self._http_base}: {e}") from e

        if resp.status_code != 200:
            raise RuntimeError(
                f"Turso HTTP error {resp.status_code} for query {sql[:80]!r}: "
                f"{resp.text[:500]}"
            )

        data = resp.json()
        entry = data["results"][0]
        if entry["type"] == "error":
            err = entry.get("error", {})
            raise RuntimeError(
                f"Turso query failed for {sql[:80]!r}: {err.get('message', err)}"
            )

        result = entry["response"]["result"]
        columns = [c.get("name") for c in result.get("cols", [])]
        rows = [tuple(_from_turso_value(v) for v in row) for row in result.get("rows", [])]

        lastrowid = result.get("last_insert_rowid")
        if lastrowid is not None:
            lastrowid = int(lastrowid)

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
        # Each Turso HTTP pipeline call above already commits that
        # statement server-side before returning — there's no local
        # transaction buffer to flush, so this is a no-op on that path.

    def close(self):
        if self._driver == "sqlite":
            try:
                self._raw.close()
            except Exception:
                pass
        # The Turso path holds no persistent connection between calls
        # (each execute() is its own HTTP request that explicitly closes
        # itself), so there's nothing to release here.


def get_db() -> DBConn:
    if USE_TURSO:
        raw_url = os.environ["TURSO_DATABASE_URL"]
        token = os.environ.get("TURSO_AUTH_TOKEN")
        if not token:
            raise RuntimeError(
                "TURSO_DATABASE_URL is set but TURSO_AUTH_TOKEN is missing — "
                "both are required. Check your Render environment variables."
            )
        return DBConn("turso", http_base=_turso_http_base_url(raw_url), http_token=token)
    else:
        conn = sqlite3.connect(LOCAL_DB_PATH)
        return DBConn("sqlite", raw=conn)


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
