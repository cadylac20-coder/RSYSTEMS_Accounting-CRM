"""
Accounting CRM — backend
Mirrors the structure/conventions of the Visa CRM (FastAPI + single-page
admin.html frontend), adapted for an accounting/compliance practice.
Deploys on Render; persists to Turso (libSQL) in production, local SQLite
in dev — see database.py.
"""
import os
import json
import uuid
import tempfile
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel

from database import init_db, get_db
from auth import (
    hash_password, verify_password, issue_token,
    require_admin, require_roles, ALL_ROLES,
)

IST = ZoneInfo("Asia/Kolkata")


def now_ist() -> datetime:
    return datetime.now(IST)


def now_ist_str() -> str:
    return now_ist().strftime("%Y-%m-%d %H:%M:%S")


def today_ist_str() -> str:
    return now_ist().strftime("%Y-%m-%d")


app = FastAPI(title="Accounting CRM — Rsystems")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

init_db()


def _seed_superadmin():
    """First-run convenience: if there are no staff accounts yet, create one
    superadmin from env vars so you're never locked out of a fresh deploy."""
    conn = get_db()
    count = conn.execute("SELECT COUNT(*) as c FROM admin_users").fetchone()["c"]
    if count == 0:
        email = os.environ.get("SEED_ADMIN_EMAIL", "admin@rsystems.in").strip().lower()
        password = os.environ.get("SEED_ADMIN_PASSWORD", "ChangeMe123!")
        conn.execute(
            "INSERT INTO admin_users (name,email,password_hash,role,active,created_at) VALUES (?,?,?,?,1,?)",
            ("Super Admin", email, hash_password(password), "superadmin", now_ist_str())
        )
        conn.commit()
    conn.close()


_seed_superadmin()

# ── Static frontend ──────────────────────────────────────────────────────────
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
def root():
    return RedirectResponse("/static/admin.html")


# ── Shared helpers ───────────────────────────────────────────────────────────
def _log_staff_activity(admin: dict, action: str, detail: str = "", session_id: str = ""):
    conn = get_db()
    conn.execute(
        "INSERT INTO staff_activity_log (staff_id, staff_name, staff_role, action, detail, session_id, created_at) VALUES (?,?,?,?,?,?,?)",
        (admin.get("id"), admin.get("name"), admin.get("role"), action, detail, session_id, now_ist_str())
    )
    conn.commit()
    conn.close()


class LogActivityRequest(BaseModel):
    action: str
    detail: Optional[str] = ""
    session_id: Optional[str] = ""


@app.post("/admin/log-activity")
def log_activity(data: LogActivityRequest, admin=Depends(require_admin)):
    _log_staff_activity(admin, data.action, data.detail or "", data.session_id or "")
    return {"status": "logged"}


# ══════════════════════════════════════════════════════════════════════════
# AUTH
# ══════════════════════════════════════════════════════════════════════════
class LoginRequest(BaseModel):
    email: str
    password: str


@app.post("/auth/admin/login")
def login(data: LoginRequest):
    conn = get_db()
    row = conn.execute("SELECT * FROM admin_users WHERE email=?", (data.email.strip().lower(),)).fetchone()
    conn.close()
    if not row or not row["active"] or not verify_password(data.password, row["password_hash"]):
        raise HTTPException(401, "Invalid email or password")
    token = issue_token(row["id"])
    _log_staff_activity(dict(row), "Logged in")
    return {
        "token": token, "id": row["id"], "name": row["name"],
        "role": row["role"], "email": row["email"],
    }


@app.get("/auth/me")
def whoami(admin=Depends(require_admin)):
    """Lets the frontend restore session state (name/role/id) after a page
    reload, using only the token it already has in localStorage."""
    return {"id": admin["id"], "name": admin["name"], "role": admin["role"], "email": admin["email"]}


# ══════════════════════════════════════════════════════════════════════════
# STAFF MANAGEMENT (superadmin only for full CRUD)
# ══════════════════════════════════════════════════════════════════════════
class NewStaffRequest(BaseModel):
    name: str
    email: str
    password: str
    role: str = "staff"


class UpdateStaffRequest(BaseModel):
    name: Optional[str] = None
    email: Optional[str] = None
    role: Optional[str] = None
    active: Optional[bool] = None
    password: Optional[str] = None


@app.get("/admin/staff")
def list_staff(admin=Depends(require_roles("superadmin"))):
    conn = get_db()
    rows = conn.execute("SELECT id,name,email,role,active,created_at FROM admin_users ORDER BY name").fetchall()
    conn.close()
    return {"staff": [dict(r) for r in rows]}


@app.get("/admin/staff/assignable")
def list_assignable_staff(admin=Depends(require_admin)):
    """Lightweight roster (id/name/role only) for assignment dropdowns —
    available to any logged-in staff member, not just superadmin."""
    conn = get_db()
    rows = conn.execute("SELECT id,name,role FROM admin_users WHERE active=1 ORDER BY name").fetchall()
    conn.close()
    return {"staff": [dict(r) for r in rows]}


@app.post("/admin/staff")
def create_staff(data: NewStaffRequest, admin=Depends(require_roles("superadmin"))):
    if data.role not in ALL_ROLES:
        raise HTTPException(400, f"role must be one of {ALL_ROLES}")
    conn = get_db()
    existing = conn.execute("SELECT id FROM admin_users WHERE email=?", (data.email.strip().lower(),)).fetchone()
    if existing:
        conn.close()
        raise HTTPException(400, "A staff account with that email already exists")
    conn.execute(
        "INSERT INTO admin_users (name,email,password_hash,role,active,created_at) VALUES (?,?,?,?,1,?)",
        (data.name.strip(), data.email.strip().lower(), hash_password(data.password), data.role, now_ist_str())
    )
    conn.commit()
    conn.close()
    return {"status": "created"}


@app.put("/admin/staff/{staff_id}")
def update_staff(staff_id: int, data: UpdateStaffRequest, admin=Depends(require_roles("superadmin"))):
    conn = get_db()
    row = conn.execute("SELECT * FROM admin_users WHERE id=?", (staff_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "Staff member not found")
    sets, values = [], []
    if data.name is not None:
        sets.append("name=?"); values.append(data.name.strip())
    if data.email is not None:
        sets.append("email=?"); values.append(data.email.strip().lower())
    if data.role is not None:
        if data.role not in ALL_ROLES:
            conn.close()
            raise HTTPException(400, f"role must be one of {ALL_ROLES}")
        sets.append("role=?"); values.append(data.role)
    if data.active is not None:
        sets.append("active=?"); values.append(1 if data.active else 0)
    if data.password:
        sets.append("password_hash=?"); values.append(hash_password(data.password))
    if sets:
        values.append(staff_id)
        conn.execute(f"UPDATE admin_users SET {', '.join(sets)} WHERE id=?", values)
        conn.commit()
    conn.close()
    return {"status": "updated"}


@app.delete("/admin/staff/{staff_id}")
def delete_staff(staff_id: int, admin=Depends(require_roles("superadmin"))):
    if staff_id == admin["id"]:
        raise HTTPException(400, "You can't delete your own account")
    conn = get_db()
    conn.execute("UPDATE admin_users SET active=0 WHERE id=?", (staff_id,))
    conn.commit()
    conn.close()
    return {"status": "deactivated"}


# ══════════════════════════════════════════════════════════════════════════
# CLIENTS
# ══════════════════════════════════════════════════════════════════════════
class NewClientRequest(BaseModel):
    name: str
    entity_type: str = "individual"
    pan: Optional[str] = None
    gstin: Optional[str] = None
    tan: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    address: Optional[str] = None
    services: list = []
    assigned_to: Optional[int] = None
    notes: Optional[str] = None


class UpdateClientRequest(BaseModel):
    name: Optional[str] = None
    entity_type: Optional[str] = None
    pan: Optional[str] = None
    gstin: Optional[str] = None
    tan: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    address: Optional[str] = None
    services: Optional[list] = None
    status: Optional[str] = None
    notes: Optional[str] = None


def _client_out(row) -> dict:
    c = dict(row)
    c["services"] = json.loads(c.pop("services_json") or "[]")
    return c


@app.get("/admin/clients")
def list_clients(admin=Depends(require_admin)):
    conn = get_db()
    rows = conn.execute("""
        SELECT c.*, a.name as assigned_name
        FROM clients c LEFT JOIN admin_users a ON a.id = c.assigned_to
        ORDER BY c.name
    """).fetchall()
    conn.close()
    return {"clients": [_client_out(r) for r in rows]}


@app.get("/admin/client/{client_id}")
def get_client(client_id: int, admin=Depends(require_admin)):
    conn = get_db()
    row = conn.execute("""
        SELECT c.*, a.name as assigned_name
        FROM clients c LEFT JOIN admin_users a ON a.id = c.assigned_to
        WHERE c.id=?
    """, (client_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "Client not found")
    tasks = conn.execute("SELECT * FROM compliance_tasks WHERE client_id=? ORDER BY due_date DESC LIMIT 20", (client_id,)).fetchall()
    invoices = conn.execute("SELECT * FROM invoices WHERE client_id=? ORDER BY issue_date DESC LIMIT 20", (client_id,)).fetchall()
    conn.close()
    return {
        "client": _client_out(row),
        "compliance_tasks": [dict(t) for t in tasks],
        "invoices": [dict(i) for i in invoices],
    }


@app.post("/admin/client")
def create_client(data: NewClientRequest, admin=Depends(require_admin)):
    conn = get_db()
    ts = now_ist_str()
    cur = conn.execute("""
        INSERT INTO clients (name,entity_type,pan,gstin,tan,email,phone,address,services_json,assigned_to,status,notes,created_at,updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,'active',?,?,?)
    """, (data.name.strip(), data.entity_type, data.pan, data.gstin, data.tan, data.email, data.phone,
          data.address, json.dumps(data.services), data.assigned_to, data.notes, ts, ts))
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return {"status": "created", "id": new_id}


@app.put("/admin/client/{client_id}")
def update_client(client_id: int, data: UpdateClientRequest, admin=Depends(require_admin)):
    conn = get_db()
    row = conn.execute("SELECT id FROM clients WHERE id=?", (client_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "Client not found")
    sets, values = ["updated_at=?"], [now_ist_str()]
    fields = {
        "name": data.name, "entity_type": data.entity_type, "pan": data.pan,
        "gstin": data.gstin, "tan": data.tan, "email": data.email, "phone": data.phone,
        "address": data.address, "status": data.status, "notes": data.notes,
    }
    for col, val in fields.items():
        if val is not None:
            sets.append(f"{col}=?"); values.append(val)
    if data.services is not None:
        sets.append("services_json=?"); values.append(json.dumps(data.services))
    values.append(client_id)
    conn.execute(f"UPDATE clients SET {', '.join(sets)} WHERE id=?", values)
    conn.commit()
    conn.close()
    return {"status": "updated"}


class AssignClientRequest(BaseModel):
    assigned_to: Optional[int] = None


@app.put("/admin/client/{client_id}/assign")
def assign_client(client_id: int, data: AssignClientRequest, admin=Depends(require_roles("admin"))):
    """Assigning clients to staff is restricted to admin/superadmin."""
    conn = get_db()
    row = conn.execute("SELECT id FROM clients WHERE id=?", (client_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "Client not found")
    if data.assigned_to is not None:
        staff = conn.execute("SELECT id FROM admin_users WHERE id=? AND active=1", (data.assigned_to,)).fetchone()
        if not staff:
            conn.close()
            raise HTTPException(400, "Selected staff member not found or inactive")
    conn.execute("UPDATE clients SET assigned_to=?, updated_at=? WHERE id=?", (data.assigned_to, now_ist_str(), client_id))
    conn.commit()
    conn.close()
    return {"status": "assigned"}


@app.delete("/admin/client/{client_id}")
def delete_client(client_id: int, admin=Depends(require_roles("admin"))):
    conn = get_db()
    conn.execute("UPDATE clients SET status='archived', updated_at=? WHERE id=?", (now_ist_str(), client_id))
    conn.commit()
    conn.close()
    return {"status": "archived"}


# ══════════════════════════════════════════════════════════════════════════
# LEADS
# ══════════════════════════════════════════════════════════════════════════
class NewLeadRequest(BaseModel):
    name: str
    company: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    service_interested: Optional[str] = None
    source: str = "other"
    notes: Optional[str] = None


class UpdateLeadRequest(BaseModel):
    name: Optional[str] = None
    company: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    service_interested: Optional[str] = None
    status: Optional[str] = None
    notes: Optional[str] = None


class AssignLeadRequest(BaseModel):
    assigned_to: Optional[int] = None


@app.get("/admin/leads")
def list_leads(admin=Depends(require_admin)):
    conn = get_db()
    rows = conn.execute("""
        SELECT l.*, a.name as assigned_name
        FROM leads l LEFT JOIN admin_users a ON a.id = l.assigned_to
        ORDER BY l.created_at DESC
    """).fetchall()
    conn.close()
    return {"leads": [dict(r) for r in rows]}


@app.post("/admin/lead")
def create_lead(data: NewLeadRequest, admin=Depends(require_admin)):
    conn = get_db()
    ts = now_ist_str()
    cur = conn.execute("""
        INSERT INTO leads (name,company,phone,email,service_interested,source,status,notes,created_at,updated_at)
        VALUES (?,?,?,?,?,?,'new',?,?,?)
    """, (data.name.strip(), data.company, data.phone, data.email, data.service_interested, data.source, data.notes, ts, ts))
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return {"status": "created", "id": new_id}


@app.put("/admin/lead/{lead_id}")
def update_lead(lead_id: int, data: UpdateLeadRequest, admin=Depends(require_admin)):
    conn = get_db()
    row = conn.execute("SELECT id FROM leads WHERE id=?", (lead_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "Lead not found")
    sets, values = ["updated_at=?"], [now_ist_str()]
    fields = {
        # assigned_to intentionally excluded — handled by the dedicated,
        # admin-restricted /assign endpoint below.
        "name": data.name, "company": data.company, "phone": data.phone,
        "email": data.email, "service_interested": data.service_interested,
        "status": data.status, "notes": data.notes,
    }
    for col, val in fields.items():
        if val is not None:
            sets.append(f"{col}=?"); values.append(val)
    values.append(lead_id)
    conn.execute(f"UPDATE leads SET {', '.join(sets)} WHERE id=?", values)
    conn.commit()
    conn.close()
    return {"status": "updated"}


@app.put("/admin/lead/{lead_id}/assign")
def assign_lead(lead_id: int, data: AssignLeadRequest, admin=Depends(require_roles("admin"))):
    conn = get_db()
    row = conn.execute("SELECT id FROM leads WHERE id=?", (lead_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "Lead not found")
    if data.assigned_to is not None:
        staff = conn.execute("SELECT id FROM admin_users WHERE id=? AND active=1", (data.assigned_to,)).fetchone()
        if not staff:
            conn.close()
            raise HTTPException(400, "Selected staff member not found or inactive")
    conn.execute("UPDATE leads SET assigned_to=?, updated_at=? WHERE id=?", (data.assigned_to, now_ist_str(), lead_id))
    conn.commit()
    conn.close()
    return {"status": "assigned"}


@app.post("/admin/lead/{lead_id}/convert")
def convert_lead(lead_id: int, admin=Depends(require_admin)):
    conn = get_db()
    lead = conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
    if not lead:
        conn.close()
        raise HTTPException(404, "Lead not found")
    if lead["converted_client_id"]:
        conn.close()
        raise HTTPException(400, "Lead already converted")
    ts = now_ist_str()
    cur = conn.execute("""
        INSERT INTO clients (name,entity_type,email,phone,services_json,assigned_to,status,notes,created_at,updated_at)
        VALUES (?,?,?,?,?,?,'active',?,?,?)
    """, (lead["name"], "individual", lead["email"], lead["phone"],
          json.dumps([lead["service_interested"]] if lead["service_interested"] else []),
          lead["assigned_to"], lead["notes"], ts, ts))
    client_id = cur.lastrowid
    conn.execute("UPDATE leads SET status='won', converted_client_id=?, updated_at=? WHERE id=?", (client_id, ts, lead_id))
    conn.commit()
    conn.close()
    return {"status": "converted", "client_id": client_id}


@app.delete("/admin/lead/{lead_id}")
def delete_lead(lead_id: int, admin=Depends(require_admin)):
    conn = get_db()
    conn.execute("DELETE FROM leads WHERE id=?", (lead_id,))
    conn.commit()
    conn.close()
    return {"status": "deleted"}


# ══════════════════════════════════════════════════════════════════════════
# COMPLIANCE — task types (GSTR-1, GSTR-3B, TDS, ITR, ROC...), per-client
# tasks with due dates/status, and recurring templates that auto-generate
# the next period's tasks. This has no equivalent in the Visa CRM — it's
# the accounting-specific core of this app.
# ══════════════════════════════════════════════════════════════════════════
class NewComplianceTaskRequest(BaseModel):
    client_id: int
    task_type: str
    period_label: str
    due_date: str
    assigned_to: Optional[int] = None
    notes: Optional[str] = None


class UpdateComplianceTaskRequest(BaseModel):
    task_type: Optional[str] = None
    period_label: Optional[str] = None
    due_date: Optional[str] = None
    status: Optional[str] = None
    assigned_to: Optional[int] = None
    documents_received: Optional[bool] = None
    notes: Optional[str] = None


@app.get("/admin/compliance-tasks")
def list_compliance_tasks(admin=Depends(require_admin), status: Optional[str] = None, client_id: Optional[int] = None):
    conn = get_db()
    sql = """
        SELECT t.*, c.name as client_name, a.name as assigned_name
        FROM compliance_tasks t
        LEFT JOIN clients c ON c.id = t.client_id
        LEFT JOIN admin_users a ON a.id = t.assigned_to
        WHERE 1=1
    """
    params = []
    if status:
        sql += " AND t.status=?"; params.append(status)
    if client_id:
        sql += " AND t.client_id=?"; params.append(client_id)
    sql += " ORDER BY t.due_date ASC"
    rows = conn.execute(sql, params).fetchall()

    # Auto-flag anything past due that's still marked pending/in_progress —
    # keeps "overdue" accurate without a background job.
    today = today_ist_str()
    out = []
    for r in rows:
        d = dict(r)
        if d["status"] in ("pending", "in_progress") and d["due_date"] < today:
            d["status"] = "overdue"
        out.append(d)
    conn.close()
    return {"tasks": out}


@app.post("/admin/compliance-task")
def create_compliance_task(data: NewComplianceTaskRequest, admin=Depends(require_admin)):
    conn = get_db()
    ts = now_ist_str()
    cur = conn.execute("""
        INSERT INTO compliance_tasks (client_id,task_type,period_label,due_date,status,assigned_to,documents_received,notes,created_at,updated_at)
        VALUES (?,?,?,?,'pending',?,0,?,?,?)
    """, (data.client_id, data.task_type, data.period_label, data.due_date, data.assigned_to, data.notes, ts, ts))
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return {"status": "created", "id": new_id}


@app.put("/admin/compliance-task/{task_id}")
def update_compliance_task(task_id: int, data: UpdateComplianceTaskRequest, admin=Depends(require_admin)):
    conn = get_db()
    row = conn.execute("SELECT id FROM compliance_tasks WHERE id=?", (task_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "Task not found")
    sets, values = ["updated_at=?"], [now_ist_str()]
    fields = {
        "task_type": data.task_type, "period_label": data.period_label,
        "due_date": data.due_date, "assigned_to": data.assigned_to, "notes": data.notes,
    }
    for col, val in fields.items():
        if val is not None:
            sets.append(f"{col}=?"); values.append(val)
    if data.status is not None:
        sets.append("status=?"); values.append(data.status)
        if data.status == "filed":
            sets.append("filed_date=?"); values.append(today_ist_str())
    if data.documents_received is not None:
        sets.append("documents_received=?"); values.append(1 if data.documents_received else 0)
    values.append(task_id)
    conn.execute(f"UPDATE compliance_tasks SET {', '.join(sets)} WHERE id=?", values)
    conn.commit()
    conn.close()
    return {"status": "updated"}


@app.delete("/admin/compliance-task/{task_id}")
def delete_compliance_task(task_id: int, admin=Depends(require_roles("admin"))):
    conn = get_db()
    conn.execute("DELETE FROM compliance_tasks WHERE id=?", (task_id,))
    conn.commit()
    conn.close()
    return {"status": "deleted"}


# ── Recurring compliance templates ──────────────────────────────────────────
class NewComplianceTemplateRequest(BaseModel):
    task_type: str
    frequency: str = "monthly"          # monthly | quarterly | annually | one_time
    due_day: int = 20                   # day-of-month the task is due
    due_month_offset: int = 1           # how many months after the period it's due
    description: Optional[str] = None


@app.get("/admin/compliance-templates")
def list_compliance_templates(admin=Depends(require_admin)):
    conn = get_db()
    rows = conn.execute("SELECT * FROM compliance_templates WHERE active=1 ORDER BY task_type").fetchall()
    conn.close()
    return {"templates": [dict(r) for r in rows]}


@app.post("/admin/compliance-template")
def create_compliance_template(data: NewComplianceTemplateRequest, admin=Depends(require_roles("admin"))):
    conn = get_db()
    cur = conn.execute("""
        INSERT INTO compliance_templates (task_type,frequency,due_day,due_month_offset,description,active,created_at)
        VALUES (?,?,?,?,?,1,?)
    """, (data.task_type, data.frequency, data.due_day, data.due_month_offset, data.description, now_ist_str()))
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return {"status": "created", "id": new_id}


@app.delete("/admin/compliance-template/{template_id}")
def delete_compliance_template(template_id: int, admin=Depends(require_roles("admin"))):
    conn = get_db()
    conn.execute("UPDATE compliance_templates SET active=0 WHERE id=?", (template_id,))
    conn.commit()
    conn.close()
    return {"status": "deactivated"}


class SubscribeComplianceRequest(BaseModel):
    template_ids: list[int]


@app.post("/admin/client/{client_id}/subscribe-compliance")
def subscribe_client_compliance(client_id: int, data: SubscribeComplianceRequest, admin=Depends(require_admin)):
    """Mark which recurring filings a client is subject to (e.g. this client
    needs GSTR-1 + GSTR-3B monthly). generate-recurring-tasks uses this list."""
    conn = get_db()
    client = conn.execute("SELECT id FROM clients WHERE id=?", (client_id,)).fetchone()
    if not client:
        conn.close()
        raise HTTPException(404, "Client not found")
    conn.execute("UPDATE client_compliance_subscriptions SET active=0 WHERE client_id=?", (client_id,))
    ts = now_ist_str()
    for tid in data.template_ids:
        conn.execute(
            "INSERT INTO client_compliance_subscriptions (client_id,template_id,active,created_at) VALUES (?,?,1,?)",
            (client_id, tid, ts)
        )
    conn.commit()
    conn.close()
    return {"status": "subscribed", "count": len(data.template_ids)}


@app.get("/admin/client/{client_id}/compliance-subscriptions")
def get_client_compliance_subscriptions(client_id: int, admin=Depends(require_admin)):
    conn = get_db()
    rows = conn.execute("""
        SELECT s.template_id, t.task_type, t.frequency, t.due_day, t.due_month_offset
        FROM client_compliance_subscriptions s
        JOIN compliance_templates t ON t.id = s.template_id
        WHERE s.client_id=? AND s.active=1
    """, (client_id,)).fetchall()
    conn.close()
    return {"subscriptions": [dict(r) for r in rows]}


def _period_label_and_due(freq: str, due_day: int, due_month_offset: int, ref: datetime):
    """Given today's date, compute the *next* period's label + due date for
    a recurring filing. Monthly -> previous calendar month; quarterly ->
    previous quarter (Apr-Jun/Jul-Sep/Oct-Dec/Jan-Mar per Indian FY);
    annually -> the just-closed financial year (Apr-Mar)."""
    if freq == "monthly":
        first_of_this_month = ref.replace(day=1)
        period_end = first_of_this_month - timedelta(days=1)   # last day of prev month
        period_label = period_end.strftime("%B %Y")
        due_month = period_end.month + due_month_offset
        due_year = period_end.year + (due_month - 1) // 12
        due_month = (due_month - 1) % 12 + 1
        due_date = f"{due_year:04d}-{due_month:02d}-{min(due_day,28):02d}"
        return period_label, due_date

    if freq == "quarterly":
        # Indian FY quarters: Q1 Apr-Jun, Q2 Jul-Sep, Q3 Oct-Dec, Q4 Jan-Mar.
        # Represent "which quarter of which FY" as one running integer index
        # so "the previous quarter" is just index-1, with no edge cases.
        fy_start_year = ref.year if ref.month >= 4 else ref.year - 1
        fm = (ref.month - 4) % 12          # 0..11, 0 = April
        q_idx = fm // 3                     # 0..3, current quarter within the FY
        prev_total = fy_start_year * 4 + q_idx - 1
        prev_fy_start_year, prev_q_idx = divmod(prev_total, 4)

        q_month_ranges = [(4,6), (7,9), (10,12), (1,3)]
        start_m, end_m = q_month_ranges[prev_q_idx]
        cal_year = prev_fy_start_year if prev_q_idx != 3 else prev_fy_start_year + 1

        period_label = f"Q{prev_q_idx+1} FY{prev_fy_start_year}-{str(prev_fy_start_year+1)[2:]} ({start_m:02d}-{end_m:02d} {cal_year})"
        due_month = end_m + due_month_offset
        due_year = cal_year + (due_month - 1) // 12
        due_month = (due_month - 1) % 12 + 1
        due_date = f"{due_year:04d}-{due_month:02d}-{min(due_day,28):02d}"
        return period_label, due_date

    if freq == "annually":
        # Indian FY: Apr(y) - Mar(y+1). If we're past March, the just-closed
        # FY is (this_year-1)-(this_year); otherwise it's (last_year-1)-(last_year).
        if ref.month >= 4:
            fy_start, fy_end = ref.year, ref.year + 1
        else:
            fy_start, fy_end = ref.year - 1, ref.year
        period_label = f"FY {fy_start}-{str(fy_end)[2:]}"
        due_month = 3 + due_month_offset  # months after FY close (March)
        due_year = fy_end + (due_month - 1) // 12
        due_month = (due_month - 1) % 12 + 1
        due_date = f"{due_year:04d}-{due_month:02d}-{min(due_day,28):02d}"
        return period_label, due_date

    # one_time — caller supplies dates manually, this generator skips these
    return None, None


@app.post("/admin/compliance/generate-recurring")
def generate_recurring_compliance_tasks(admin=Depends(require_roles("admin"))):
    """Run this monthly (or wire it to a cron hitting this endpoint) to
    create the next period's compliance task for every client subscribed
    to a recurring template, skipping ones already generated."""
    conn = get_db()
    subs = conn.execute("""
        SELECT s.client_id, t.id as template_id, t.task_type, t.frequency, t.due_day, t.due_month_offset
        FROM client_compliance_subscriptions s
        JOIN compliance_templates t ON t.id = s.template_id
        WHERE s.active=1 AND t.active=1 AND t.frequency != 'one_time'
    """).fetchall()

    ref = now_ist()
    created = 0
    ts = now_ist_str()
    for s in subs:
        period_label, due_date = _period_label_and_due(s["frequency"], s["due_day"], s["due_month_offset"], ref)
        if not period_label:
            continue
        exists = conn.execute(
            "SELECT id FROM compliance_tasks WHERE client_id=? AND task_type=? AND period_label=?",
            (s["client_id"], s["task_type"], period_label)
        ).fetchone()
        if exists:
            continue
        conn.execute("""
            INSERT INTO compliance_tasks (client_id,task_type,period_label,due_date,status,documents_received,created_at,updated_at)
            VALUES (?,?,?,?,'pending',0,?,?)
        """, (s["client_id"], s["task_type"], period_label, due_date, ts, ts))
        created += 1
    conn.commit()
    conn.close()
    return {"status": "generated", "tasks_created": created}


# ══════════════════════════════════════════════════════════════════════════
# DOCUMENT CHECKLISTS — same numbered-list / free-form-text pattern as the
# Visa CRM, scoped to accounting service types instead of countries.
# ══════════════════════════════════════════════════════════════════════════
class ChecklistData(BaseModel):
    service_type: str
    name: str
    documents: list = []
    format_mode: str = "list"
    free_text: Optional[str] = ""


class UpdateChecklistData(BaseModel):
    service_type: Optional[str] = None
    name: Optional[str] = None
    documents: Optional[list] = None
    format_mode: Optional[str] = None
    free_text: Optional[str] = None


@app.get("/admin/checklists")
def list_checklists(admin=Depends(require_admin)):
    conn = get_db()
    rows = conn.execute("SELECT * FROM checklists ORDER BY service_type, name").fetchall()
    conn.close()
    out = []
    for r in rows:
        c = dict(r)
        c["documents"] = json.loads(c.pop("documents_json") or "[]")
        out.append(c)
    return {"checklists": out}


@app.post("/admin/checklist")
def create_checklist(data: ChecklistData, admin=Depends(require_admin)):
    conn = get_db()
    cur = conn.execute("""
        INSERT INTO checklists (service_type,name,documents_json,format_mode,free_text,created_by,created_at)
        VALUES (?,?,?,?,?,?,?)
    """, (data.service_type, data.name, json.dumps(data.documents), data.format_mode, data.free_text or "", admin["name"], now_ist_str()))
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return {"status": "created", "id": new_id}


@app.put("/admin/checklist/{checklist_id}")
def update_checklist(checklist_id: int, data: UpdateChecklistData, admin=Depends(require_admin)):
    conn = get_db()
    sets, values = [], []
    if data.service_type is not None:
        sets.append("service_type=?"); values.append(data.service_type)
    if data.name is not None:
        sets.append("name=?"); values.append(data.name)
    if data.documents is not None:
        sets.append("documents_json=?"); values.append(json.dumps(data.documents))
    if data.format_mode is not None:
        sets.append("format_mode=?"); values.append(data.format_mode)
    if data.free_text is not None:
        sets.append("free_text=?"); values.append(data.free_text)
    if sets:
        values.append(checklist_id)
        conn.execute(f"UPDATE checklists SET {', '.join(sets)} WHERE id=?", values)
        conn.commit()
    conn.close()
    return {"status": "updated"}


@app.delete("/admin/checklist/{checklist_id}")
def delete_checklist(checklist_id: int, admin=Depends(require_admin)):
    conn = get_db()
    conn.execute("DELETE FROM checklists WHERE id=?", (checklist_id,))
    conn.commit()
    conn.close()
    return {"status": "deleted"}


class ExportChecklistsRequest(BaseModel):
    checklist_ids: list[int]
    letter_template_ids: list[int] = []


@app.post("/admin/checklists/export-pdf")
def export_checklists_pdf(data: ExportChecklistsRequest, admin=Depends(require_admin)):
    if not data.checklist_ids:
        raise HTTPException(400, "Select at least one checklist to export")

    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.enums import TA_CENTER
    from xml.sax.saxutils import escape as _esc

    conn = get_db()
    placeholders = ",".join("?" * len(data.checklist_ids))
    rows = conn.execute(f"SELECT * FROM checklists WHERE id IN ({placeholders}) ORDER BY service_type", data.checklist_ids).fetchall()

    letter_rows = []
    if data.letter_template_ids:
        lt_ph = ",".join("?" * len(data.letter_template_ids))
        letter_rows = conn.execute(f"SELECT * FROM letter_templates WHERE id IN ({lt_ph})", data.letter_template_ids).fetchall()
    conn.close()

    if not rows:
        raise HTTPException(404, "No matching checklists found")

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
    doc = SimpleDocTemplate(tmp.name, pagesize=A4, topMargin=20*mm, bottomMargin=20*mm, leftMargin=20*mm, rightMargin=20*mm)
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("Title2", parent=styles["Title"], fontSize=16, textColor=colors.HexColor("#1e3a5f"), alignment=TA_CENTER, spaceAfter=4)
    subtitle_style = ParagraphStyle("Sub2", parent=styles["Normal"], fontSize=10, textColor=colors.HexColor("#666666"), alignment=TA_CENTER, spaceAfter=16)
    section_style = ParagraphStyle("Sec2", parent=styles["Heading2"], fontSize=12, textColor=colors.HexColor("#1e3a5f"), spaceBefore=10, spaceAfter=8)
    doc_item_style = ParagraphStyle("Item2", parent=styles["Normal"], fontSize=10.5, leading=16, spaceAfter=4)
    freeform_style = ParagraphStyle("Free2", parent=styles["Normal"], fontSize=10.5, leading=15, spaceAfter=6)
    footer_style = ParagraphStyle("Foot2", parent=styles["Normal"], fontSize=8, textColor=colors.HexColor("#888888"), spaceBefore=20)

    total_pages = len(rows) + len(letter_rows)
    story, page_num = [], 0

    for row in rows:
        page_num += 1
        cl = dict(row)
        documents = json.loads(cl["documents_json"])
        story.append(Paragraph("RSYSTEMS ACCOUNTING & COMPLIANCE", title_style))
        story.append(Paragraph("Document Checklist", subtitle_style))
        header_table = Table([
            ["Service:", cl["service_type"], "Checklist:", cl["name"]],
            ["Client Name:", "_" * 28, "PAN / GSTIN:", "_" * 20],
        ], colWidths=[28*mm, 62*mm, 28*mm, 52*mm])
        header_table.setStyle(TableStyle([
            ("FONTSIZE", (0,0), (-1,-1), 9.5),
            ("FONTNAME", (0,0), (0,-1), "Helvetica-Bold"),
            ("FONTNAME", (2,0), (2,-1), "Helvetica-Bold"),
            ("BOTTOMPADDING", (0,0), (-1,-1), 6), ("TOPPADDING", (0,0), (-1,-1), 6),
            ("LINEBELOW", (0,0), (-1,-1), 0.5, colors.HexColor("#dddddd")),
        ]))
        story.append(header_table)
        story.append(Spacer(1, 14))
        story.append(Paragraph("Required Documents", section_style))

        if cl.get("format_mode") == "freeform" and (cl.get("free_text") or "").strip():
            for line in cl["free_text"].split("\n"):
                story.append(Spacer(1, 8) if line.strip() == "" else Paragraph(_esc(line), freeform_style))
        else:
            for idx, item in enumerate(documents, 1):
                story.append(Paragraph(f"{idx}. [ ] {_esc(str(item))}", doc_item_style))

        story.append(Paragraph("Please submit originals or self-attested copies as applicable. Incomplete submissions may delay filing.", footer_style))
        if page_num < total_pages:
            story.append(PageBreak())

    for lrow in letter_rows:
        page_num += 1
        lt = dict(lrow)
        story.append(Paragraph("RSYSTEMS ACCOUNTING & COMPLIANCE", title_style))
        story.append(Paragraph(f"{lt['template_type'].replace('_',' ').title()}", subtitle_style))
        if lt.get("subject"):
            story.append(Paragraph(_esc(lt["subject"]), section_style))
        for line in (lt.get("body_template") or "").split("\n"):
            story.append(Spacer(1, 8) if line.strip() == "" else Paragraph(_esc(line), freeform_style))
        if page_num < total_pages:
            story.append(PageBreak())

    doc.build(story)
    filename = "checklist_export.pdf" if total_pages > 1 else f"{rows[0]['service_type']}_checklist.pdf"
    return FileResponse(tmp.name, filename=filename, media_type="application/pdf")


# ══════════════════════════════════════════════════════════════════════════
# INVOICES + CLIENT LEDGER
# ══════════════════════════════════════════════════════════════════════════
class InvoiceItem(BaseModel):
    description: str
    amount: float


class NewInvoiceRequest(BaseModel):
    client_id: int
    items: list[InvoiceItem]
    tax_percent: float = 18.0
    due_date: Optional[str] = None
    notes: Optional[str] = None


class RecordPaymentRequest(BaseModel):
    amount: float
    entry_date: Optional[str] = None
    notes: Optional[str] = None


def _next_invoice_no(conn) -> str:
    year = now_ist().strftime("%y")
    row = conn.execute("SELECT COUNT(*) as c FROM invoices WHERE invoice_no LIKE ?", (f"RSYS/{year}/%",)).fetchone()
    seq = (row["c"] or 0) + 1
    return f"RSYS/{year}/{seq:04d}"


@app.get("/admin/invoices")
def list_invoices(admin=Depends(require_admin), status: Optional[str] = None):
    conn = get_db()
    sql = "SELECT i.*, c.name as client_name FROM invoices i LEFT JOIN clients c ON c.id = i.client_id"
    params = []
    if status:
        sql += " WHERE i.status=?"; params.append(status)
    sql += " ORDER BY i.issue_date DESC"
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return {"invoices": [dict(r) for r in rows]}


@app.post("/admin/invoice")
def create_invoice(data: NewInvoiceRequest, admin=Depends(require_admin)):
    if not data.items:
        raise HTTPException(400, "Add at least one line item")
    conn = get_db()
    client = conn.execute("SELECT id FROM clients WHERE id=?", (data.client_id,)).fetchone()
    if not client:
        conn.close()
        raise HTTPException(404, "Client not found")

    subtotal = sum(i.amount for i in data.items)
    tax_amount = round(subtotal * data.tax_percent / 100, 2)
    total = round(subtotal + tax_amount, 2)
    invoice_no = _next_invoice_no(conn)
    ts = now_ist_str()

    cur = conn.execute("""
        INSERT INTO invoices (invoice_no,client_id,items_json,subtotal,tax_amount,total,status,issue_date,due_date,paid_amount,notes,created_by,created_at)
        VALUES (?,?,?,?,?,?,'sent',?,?,0,?,?,?)
    """, (invoice_no, data.client_id, json.dumps([i.dict() for i in data.items]), subtotal, tax_amount, total,
          today_ist_str(), data.due_date, data.notes, admin["name"], ts))
    invoice_id = cur.lastrowid

    conn.execute("""
        INSERT INTO ledger_entries (client_id,entry_type,amount,ref_invoice_id,description,entry_date,created_by,created_at)
        VALUES (?, 'invoice', ?, ?, ?, ?, ?, ?)
    """, (data.client_id, total, invoice_id, f"Invoice {invoice_no}", today_ist_str(), admin["name"], ts))

    conn.commit()
    conn.close()
    return {"status": "created", "id": invoice_id, "invoice_no": invoice_no}


@app.post("/admin/invoice/{invoice_id}/payment")
def record_payment(invoice_id: int, data: RecordPaymentRequest, admin=Depends(require_admin)):
    conn = get_db()
    inv = conn.execute("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone()
    if not inv:
        conn.close()
        raise HTTPException(404, "Invoice not found")

    new_paid = inv["paid_amount"] + data.amount
    new_status = "paid" if new_paid >= inv["total"] else "partial"
    ts = now_ist_str()
    entry_date = data.entry_date or today_ist_str()

    conn.execute(
        "UPDATE invoices SET paid_amount=?, status=?, paid_date=? WHERE id=?",
        (new_paid, new_status, entry_date if new_status == "paid" else inv["paid_date"], invoice_id)
    )
    conn.execute("""
        INSERT INTO ledger_entries (client_id,entry_type,amount,ref_invoice_id,description,entry_date,created_by,created_at)
        VALUES (?, 'payment', ?, ?, ?, ?, ?, ?)
    """, (inv["client_id"], -data.amount, invoice_id, data.notes or f"Payment against {inv['invoice_no']}", entry_date, admin["name"], ts))
    conn.commit()
    conn.close()
    return {"status": "recorded", "new_status": new_status}


@app.get("/admin/client/{client_id}/ledger")
def get_client_ledger(client_id: int, admin=Depends(require_admin)):
    conn = get_db()
    rows = conn.execute("SELECT * FROM ledger_entries WHERE client_id=? ORDER BY entry_date, id", (client_id,)).fetchall()
    conn.close()
    balance = 0.0
    out = []
    for r in rows:
        d = dict(r)
        balance += d["amount"]
        d["running_balance"] = round(balance, 2)
        out.append(d)
    return {"entries": out, "balance": round(balance, 2)}


@app.get("/admin/invoice/{invoice_id}/pdf")
def export_invoice_pdf(invoice_id: int, admin=Depends(require_admin)):
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from xml.sax.saxutils import escape as _esc

    conn = get_db()
    inv = conn.execute("SELECT i.*, c.name as client_name, c.address, c.gstin, c.pan FROM invoices i LEFT JOIN clients c ON c.id=i.client_id WHERE i.id=?", (invoice_id,)).fetchone()
    conn.close()
    if not inv:
        raise HTTPException(404, "Invoice not found")
    inv = dict(inv)
    items = json.loads(inv["items_json"])

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
    doc = SimpleDocTemplate(tmp.name, pagesize=A4, topMargin=20*mm, bottomMargin=20*mm, leftMargin=20*mm, rightMargin=20*mm)
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("InvTitle", parent=styles["Title"], fontSize=18, textColor=colors.HexColor("#1e3a5f"))
    normal = styles["Normal"]

    story = [
        Paragraph("Rsystems Accounting & Compliance", title_style),
        Paragraph(f"Invoice {_esc(inv['invoice_no'])}", styles["Heading2"]),
        Spacer(1, 8),
        Paragraph(f"Bill To: {_esc(inv['client_name'] or '')}", normal),
        Paragraph(f"GSTIN: {_esc(inv.get('gstin') or '—')}   PAN: {_esc(inv.get('pan') or '—')}", normal),
        Paragraph(f"Issue Date: {_esc(inv['issue_date'])}   Due Date: {_esc(inv.get('due_date') or '—')}", normal),
        Spacer(1, 16),
    ]

    table_data = [["Description", "Amount (INR)"]] + [[_esc(it["description"]), f"{it['amount']:.2f}"] for it in items]
    table_data.append(["Subtotal", f"{inv['subtotal']:.2f}"])
    table_data.append(["Tax", f"{inv['tax_amount']:.2f}"])
    table_data.append(["Total", f"{inv['total']:.2f}"])
    t = Table(table_data, colWidths=[130*mm, 40*mm])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#1e3a5f")),
        ("TEXTCOLOR", (0,0), (-1,0), colors.white),
        ("FONTNAME", (0,-3), (-1,-1), "Helvetica-Bold"),
        ("LINEABOVE", (0,-3), (-1,-3), 0.5, colors.grey),
        ("BOTTOMPADDING", (0,0), (-1,-1), 6), ("TOPPADDING", (0,0), (-1,-1), 6),
        ("ALIGN", (1,0), (1,-1), "RIGHT"),
    ]))
    story.append(t)
    story.append(Spacer(1, 16))
    story.append(Paragraph(f"Status: {inv['status'].upper()}   Paid: INR {inv['paid_amount']:.2f}", normal))
    if inv.get("notes"):
        story.append(Spacer(1, 10))
        story.append(Paragraph(_esc(inv["notes"]), normal))

    doc.build(story)
    return FileResponse(tmp.name, filename=f"{inv['invoice_no'].replace('/','-')}.pdf", media_type="application/pdf")


# ══════════════════════════════════════════════════════════════════════════
# NOTICES — government/department notices per client (income tax, GST) and
# their response deadlines. Another accounting-specific addition.
# ══════════════════════════════════════════════════════════════════════════
class NewNoticeRequest(BaseModel):
    client_id: int
    notice_type: str
    notice_ref_no: Optional[str] = None
    received_date: str
    response_due_date: Optional[str] = None
    description: Optional[str] = None
    assigned_to: Optional[int] = None


class UpdateNoticeRequest(BaseModel):
    status: Optional[str] = None
    response_due_date: Optional[str] = None
    description: Optional[str] = None
    assigned_to: Optional[int] = None


@app.get("/admin/notices")
def list_notices(admin=Depends(require_admin)):
    conn = get_db()
    rows = conn.execute("""
        SELECT n.*, c.name as client_name, a.name as assigned_name
        FROM notices n
        LEFT JOIN clients c ON c.id = n.client_id
        LEFT JOIN admin_users a ON a.id = n.assigned_to
        ORDER BY n.response_due_date ASC
    """).fetchall()
    conn.close()
    return {"notices": [dict(r) for r in rows]}


@app.post("/admin/notice")
def create_notice(data: NewNoticeRequest, admin=Depends(require_admin)):
    conn = get_db()
    ts = now_ist_str()
    cur = conn.execute("""
        INSERT INTO notices (client_id,notice_type,notice_ref_no,received_date,response_due_date,status,description,assigned_to,created_at,updated_at)
        VALUES (?,?,?,?,?,'open',?,?,?,?)
    """, (data.client_id, data.notice_type, data.notice_ref_no, data.received_date, data.response_due_date, data.description, data.assigned_to, ts, ts))
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return {"status": "created", "id": new_id}


@app.put("/admin/notice/{notice_id}")
def update_notice(notice_id: int, data: UpdateNoticeRequest, admin=Depends(require_admin)):
    conn = get_db()
    sets, values = ["updated_at=?"], [now_ist_str()]
    fields = {"status": data.status, "response_due_date": data.response_due_date,
              "description": data.description, "assigned_to": data.assigned_to}
    for col, val in fields.items():
        if val is not None:
            sets.append(f"{col}=?"); values.append(val)
    values.append(notice_id)
    conn.execute(f"UPDATE notices SET {', '.join(sets)} WHERE id=?", values)
    conn.commit()
    conn.close()
    return {"status": "updated"}


# ══════════════════════════════════════════════════════════════════════════
# LETTER TEMPLATES (engagement letters, fee reminders, notice replies...)
# ══════════════════════════════════════════════════════════════════════════
class LetterTemplateData(BaseModel):
    template_type: str
    subject: Optional[str] = None
    body_template: str


@app.get("/admin/letter-templates")
def list_letter_templates(admin=Depends(require_admin)):
    conn = get_db()
    rows = conn.execute("SELECT * FROM letter_templates ORDER BY template_type").fetchall()
    conn.close()
    return {"templates": [dict(r) for r in rows]}


@app.post("/admin/letter-templates")
def create_letter_template(data: LetterTemplateData, admin=Depends(require_admin)):
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO letter_templates (template_type,subject,body_template,created_by,created_at) VALUES (?,?,?,?,?)",
        (data.template_type, data.subject, data.body_template, admin["name"], now_ist_str())
    )
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return {"status": "created", "id": new_id}


@app.put("/admin/letter-templates/{template_id}")
def update_letter_template(template_id: int, data: LetterTemplateData, admin=Depends(require_admin)):
    conn = get_db()
    row = conn.execute("SELECT id FROM letter_templates WHERE id=?", (template_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "Template not found")
    conn.execute(
        "UPDATE letter_templates SET template_type=?, subject=?, body_template=? WHERE id=?",
        (data.template_type, data.subject, data.body_template, template_id)
    )
    conn.commit()
    conn.close()
    return {"status": "updated"}


@app.delete("/admin/letter-templates/{template_id}")
def delete_letter_template(template_id: int, admin=Depends(require_admin)):
    conn = get_db()
    conn.execute("DELETE FROM letter_templates WHERE id=?", (template_id,))
    conn.commit()
    conn.close()
    return {"status": "deleted"}


# ══════════════════════════════════════════════════════════════════════════
# CALENDAR — all comparisons done in IST from the start (learned the hard
# way on the Visa CRM: never compare against a driver's raw "now").
# ══════════════════════════════════════════════════════════════════════════
class NewCalendarEventRequest(BaseModel):
    title: str
    description: Optional[str] = None
    event_type: str = "general"
    client_id: Optional[int] = None
    start_at: str
    reminder_minutes_before: Optional[int] = None
    reminder_email: Optional[str] = None


@app.get("/admin/calendar/events")
def list_calendar_events(admin=Depends(require_admin), start: Optional[str] = None, end: Optional[str] = None):
    conn = get_db()
    sql = "SELECT e.*, c.name as client_name FROM calendar_events e LEFT JOIN clients c ON c.id=e.client_id WHERE 1=1"
    params = []
    if start:
        sql += " AND e.start_at >= ?"; params.append(start)
    if end:
        sql += " AND e.start_at <= ?"; params.append(end)
    sql += " ORDER BY e.start_at ASC"
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return {"events": [dict(r) for r in rows]}


@app.get("/admin/calendar/upcoming")
def upcoming_calendar_events(admin=Depends(require_admin)):
    conn = get_db()
    now_str = now_ist_str()
    later_str = (now_ist() + timedelta(days=14)).strftime("%Y-%m-%dT%H:%M:%S")
    rows = conn.execute("""
        SELECT e.*, c.name as client_name
        FROM calendar_events e LEFT JOIN clients c ON c.id = e.client_id
        WHERE e.start_at >= ? AND e.start_at <= ?
        ORDER BY e.start_at ASC LIMIT 20
    """, (now_str, later_str)).fetchall()
    conn.close()
    return {"events": [dict(r) for r in rows]}


@app.post("/admin/calendar/event")
def create_calendar_event(data: NewCalendarEventRequest, admin=Depends(require_admin)):
    conn = get_db()
    cur = conn.execute("""
        INSERT INTO calendar_events (title,description,event_type,client_id,start_at,reminder_minutes_before,reminder_email,reminder_sent,created_by,created_at)
        VALUES (?,?,?,?,?,?,?,0,?,?)
    """, (data.title, data.description, data.event_type, data.client_id, data.start_at,
          data.reminder_minutes_before, data.reminder_email, admin["name"], now_ist_str()))
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return {"status": "created", "id": new_id}


@app.delete("/admin/calendar/event/{event_id}")
def delete_calendar_event(event_id: int, admin=Depends(require_admin)):
    conn = get_db()
    conn.execute("DELETE FROM calendar_events WHERE id=?", (event_id,))
    conn.commit()
    conn.close()
    return {"status": "deleted"}


# ══════════════════════════════════════════════════════════════════════════
# STAFF MONITOR
# ══════════════════════════════════════════════════════════════════════════
@app.get("/admin/staff-activity")
def get_staff_activity(admin=Depends(require_roles("admin")), limit: int = 100):
    conn = get_db()
    rows = conn.execute("SELECT * FROM staff_activity_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return {"activity": [dict(r) for r in rows]}


@app.get("/admin/staff-online")
def get_staff_online(admin=Depends(require_roles("admin"))):
    conn = get_db()
    staff = conn.execute("SELECT id,name,role FROM admin_users WHERE active=1").fetchall()
    cutoff = (now_ist() - timedelta(minutes=15)).strftime("%Y-%m-%d %H:%M:%S")
    out = []
    for s in staff:
        last = conn.execute(
            "SELECT created_at FROM staff_activity_log WHERE staff_id=? ORDER BY id DESC LIMIT 1", (s["id"],)
        ).fetchone()
        last_seen = last["created_at"] if last else None
        out.append({
            "id": s["id"], "name": s["name"], "role": s["role"],
            "last_seen": last_seen,
            "online": bool(last_seen and last_seen >= cutoff),
        })
    conn.close()
    return {"staff": out}


# ══════════════════════════════════════════════════════════════════════════
# TEAM CHAT (lightweight internal messaging, polled — no websockets)
# ══════════════════════════════════════════════════════════════════════════
class NewMessageRequest(BaseModel):
    message: str


@app.get("/admin/messages")
def get_team_messages(admin=Depends(require_admin), after_id: int = 0):
    conn = get_db()
    rows = conn.execute("SELECT * FROM team_messages WHERE id > ? ORDER BY id ASC LIMIT 200", (after_id,)).fetchall()
    conn.close()
    return {"messages": [dict(r) for r in rows]}


@app.post("/admin/messages")
def post_team_message(data: NewMessageRequest, admin=Depends(require_admin)):
    conn = get_db()
    conn.execute(
        "INSERT INTO team_messages (sender_id,sender_name,sender_role,message,created_at) VALUES (?,?,?,?,?)",
        (admin["id"], admin["name"], admin["role"], data.message, now_ist_str())
    )
    conn.commit()
    conn.close()
    return {"status": "sent"}


# ══════════════════════════════════════════════════════════════════════════
# DASHBOARD
# ══════════════════════════════════════════════════════════════════════════
@app.get("/admin/dashboard")
def dashboard(admin=Depends(require_admin)):
    conn = get_db()
    today = today_ist_str()
    month_start = now_ist().strftime("%Y-%m-01")

    total_clients = conn.execute("SELECT COUNT(*) c FROM clients WHERE status='active'").fetchone()["c"]
    pending_tasks = conn.execute("SELECT COUNT(*) c FROM compliance_tasks WHERE status IN ('pending','in_progress')").fetchone()["c"]
    overdue_tasks = conn.execute("SELECT COUNT(*) c FROM compliance_tasks WHERE status IN ('pending','in_progress') AND due_date < ?", (today,)).fetchone()["c"]
    due_this_week = conn.execute(
        "SELECT COUNT(*) c FROM compliance_tasks WHERE status IN ('pending','in_progress') AND due_date BETWEEN ? AND ?",
        (today, (now_ist() + timedelta(days=7)).strftime("%Y-%m-%d"))
    ).fetchone()["c"]
    revenue_this_month = conn.execute("SELECT COALESCE(SUM(total),0) t FROM invoices WHERE issue_date >= ?", (month_start,)).fetchone()["t"]
    outstanding = conn.execute("SELECT COALESCE(SUM(total-paid_amount),0) t FROM invoices WHERE status IN ('sent','partial','overdue')").fetchone()["t"]
    open_notices = conn.execute("SELECT COUNT(*) c FROM notices WHERE status='open'").fetchone()["c"]
    open_leads = conn.execute("SELECT COUNT(*) c FROM leads WHERE status NOT IN ('won','lost')").fetchone()["c"]

    recent_activity = conn.execute("SELECT * FROM staff_activity_log ORDER BY id DESC LIMIT 15").fetchall()
    conn.close()

    return {
        "total_clients": total_clients,
        "pending_tasks": pending_tasks,
        "overdue_tasks": overdue_tasks,
        "due_this_week": due_this_week,
        "revenue_this_month": revenue_this_month,
        "outstanding_amount": outstanding,
        "open_notices": open_notices,
        "open_leads": open_leads,
        "recent_activity": [dict(r) for r in recent_activity],
    }


# ══════════════════════════════════════════════════════════════════════════
# EXPORT
# ══════════════════════════════════════════════════════════════════════════
@app.get("/admin/export/clients-csv")
def export_clients_csv(admin=Depends(require_roles("admin"))):
    import csv, io
    conn = get_db()
    rows = conn.execute("SELECT * FROM clients ORDER BY name").fetchall()
    conn.close()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Name", "Entity Type", "PAN", "GSTIN", "TAN", "Email", "Phone", "Status"])
    for r in rows:
        d = dict(r)
        writer.writerow([d["name"], d["entity_type"], d["pan"], d["gstin"], d["tan"], d["email"], d["phone"], d["status"]])
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".csv", mode="w")
    tmp.write(buf.getvalue())
    tmp.close()
    return FileResponse(tmp.name, filename="clients_export.csv", media_type="text/csv")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
