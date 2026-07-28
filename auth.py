import os
import datetime
import bcrypt
import jwt
from fastapi import Header, HTTPException

from database import get_db

JWT_SECRET = os.environ.get("JWT_SECRET", "change-me-in-render-env-vars")
JWT_ALGO = "HS256"
TOKEN_TTL_HOURS = 12

# Ordered low -> high. require_roles(min_role) allows that role and anything above it.
ALL_ROLES = ["staff", "admin", "superadmin"]


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode(), hashed.encode())
    except Exception:
        return False


def issue_token(admin_id: int) -> str:
    payload = {
        "sub": admin_id,
        "exp": datetime.datetime.utcnow() + datetime.timedelta(hours=TOKEN_TTL_HOURS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGO)


def _decode_and_load(authorization: str | None):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing or invalid Authorization header")
    token = authorization.split(" ", 1)[1]
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGO])
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "Session expired — please log in again")
    except jwt.InvalidTokenError:
        raise HTTPException(401, "Invalid session token")

    conn = get_db()
    row = conn.execute("SELECT * FROM admin_users WHERE id=?", (payload["sub"],)).fetchone()
    conn.close()
    if not row or not row["active"]:
        raise HTTPException(401, "Account not found or deactivated")
    return dict(row)


def require_admin(authorization: str = Header(None)):
    """Any active, logged-in staff member — no role floor."""
    return _decode_and_load(authorization)


def require_roles(min_role: str):
    """Dependency factory: allows min_role and anything ranked above it
    in ALL_ROLES (e.g. require_roles("admin") lets admin + superadmin in)."""
    min_index = ALL_ROLES.index(min_role)

    def dependency(authorization: str = Header(None)):
        admin = _decode_and_load(authorization)
        role = admin.get("role")
        if role not in ALL_ROLES or ALL_ROLES.index(role) < min_index:
            raise HTTPException(403, f"Requires {min_role} role or higher")
        return admin

    return dependency
