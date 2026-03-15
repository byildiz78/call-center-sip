"""Authentication module - JWT cookie auth + user management + rate limiting."""

import os
import time
import secrets
import logging
import re
import aiosqlite
import bcrypt
import jwt
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from fastapi import Request, HTTPException, WebSocket

from bridge.config import DB_PATH

logger = logging.getLogger(__name__)

# JWT settings
_JWT_ALGORITHM = "HS256"
_JWT_EXPIRE_HOURS = 24
_COOKIE_NAME = "rcc_token"
_SECRET_KEY_FILE = "data/.jwt_secret"

# Rate limiting
_LOGIN_ATTEMPTS: dict[str, list[float]] = defaultdict(list)
_MAX_ATTEMPTS = 5
_LOCKOUT_SECONDS = 300  # 5 minutes

# Password policy
_MIN_PASSWORD_LENGTH = 8


def _get_secret_key() -> str:
    """Load or generate JWT secret key with secure file permissions."""
    try:
        if os.path.exists(_SECRET_KEY_FILE):
            with open(_SECRET_KEY_FILE, "r") as f:
                key = f.read().strip()
                if key:
                    return key
    except Exception:
        pass
    key = secrets.token_hex(32)
    os.makedirs(os.path.dirname(_SECRET_KEY_FILE) or ".", exist_ok=True)
    # Write with restrictive permissions
    fd = os.open(_SECRET_KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(key)
    return key


SECRET_KEY = _get_secret_key()

# ===== Rate Limiting =====


def _check_rate_limit(ip: str):
    """Check if IP is rate limited. Raises 429 if too many attempts."""
    now = time.time()
    # Clean old entries
    _LOGIN_ATTEMPTS[ip] = [t for t in _LOGIN_ATTEMPTS[ip] if now - t < _LOCKOUT_SECONDS]
    if len(_LOGIN_ATTEMPTS[ip]) >= _MAX_ATTEMPTS:
        remaining = int(_LOCKOUT_SECONDS - (now - _LOGIN_ATTEMPTS[ip][0]))
        logger.warning("Rate limited IP: %s (%d seconds remaining)", ip, remaining)
        raise HTTPException(
            status_code=429,
            detail=f"Cok fazla basarisiz giris. {remaining} saniye sonra tekrar deneyin.",
        )


def _record_failed_attempt(ip: str):
    _LOGIN_ATTEMPTS[ip].append(time.time())


def _clear_attempts(ip: str):
    _LOGIN_ATTEMPTS.pop(ip, None)


# ===== Password Validation =====


def validate_password(password: str) -> str | None:
    """Returns error message if password is weak, None if OK."""
    if len(password) < _MIN_PASSWORD_LENGTH:
        return f"Parola en az {_MIN_PASSWORD_LENGTH} karakter olmali"
    if not re.search(r"[a-zA-Z]", password):
        return "Parola en az bir harf icermeli"
    if not re.search(r"[0-9]", password):
        return "Parola en az bir rakam icermeli"
    return None


# ===== DB =====

_CREATE_USERS = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    must_change_pw INTEGER DEFAULT 0,
    created_at TEXT
);
"""

_ADD_MUST_CHANGE = "ALTER TABLE users ADD COLUMN must_change_pw INTEGER DEFAULT 0"


async def init_users():
    """Create users table and default admin if empty."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(_CREATE_USERS)
        try:
            await db.execute(_ADD_MUST_CHANGE)
        except Exception:
            pass
        await db.commit()
        cursor = await db.execute("SELECT COUNT(*) as c FROM users")
        row = await cursor.fetchone()
        if row[0] == 0:
            pw_hash = bcrypt.hashpw("admin123".encode(), bcrypt.gensalt()).decode()
            now = datetime.now(timezone.utc).isoformat()
            await db.execute(
                "INSERT INTO users (username, password_hash, must_change_pw, created_at) VALUES (?, ?, 1, ?)",
                ("admin", pw_hash, now),
            )
            await db.commit()
            logger.info("Default admin user created (must change password on first login)")


async def verify_user(username: str, password: str) -> dict | None:
    """Returns user dict if valid, None otherwise."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id, username, password_hash, must_change_pw FROM users WHERE username = ?",
            (username,),
        )
        row = await cursor.fetchone()
        if not row:
            logger.warning("Login failed: user '%s' not found", username)
            return None
        try:
            if bcrypt.checkpw(password.encode("utf-8"), row["password_hash"].encode("utf-8")):
                logger.info("Successful login: %s", username)
                return {"id": row["id"], "username": row["username"], "must_change_pw": bool(row["must_change_pw"])}
            logger.warning("Login failed: wrong password for '%s'", username)
            return None
        except Exception as e:
            logger.error("bcrypt error for '%s': %s", username, e)
            return None


async def list_users() -> list:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT id, username, must_change_pw, created_at FROM users ORDER BY id")
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def create_user(username: str, password: str) -> dict:
    if not re.match(r"^[a-zA-Z0-9._-]{3,30}$", username):
        raise HTTPException(400, "Kullanici adi 3-30 karakter, harf/rakam/._- icermeli")
    pw_err = validate_password(password)
    if pw_err:
        raise HTTPException(400, pw_err)
    pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute(
                "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
                (username, pw_hash, now),
            )
            await db.commit()
        except aiosqlite.IntegrityError:
            raise HTTPException(400, "Bu kullanici adi zaten mevcut")
    logger.info("User created: %s", username)
    return {"username": username, "created_at": now}


async def update_password(user_id: int, new_password: str):
    pw_err = validate_password(new_password)
    if pw_err:
        raise HTTPException(400, pw_err)
    pw_hash = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt()).decode()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "UPDATE users SET password_hash = ?, must_change_pw = 0 WHERE id = ?",
            (pw_hash, user_id),
        )
        await db.commit()
        if cursor.rowcount == 0:
            raise HTTPException(404, "Kullanici bulunamadi")
    logger.info("Password updated for user_id: %d", user_id)


async def delete_user(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT COUNT(*) FROM users")
        count = (await cursor.fetchone())[0]
        if count <= 1:
            raise HTTPException(400, "Son kullanici silinemez")
        cursor = await db.execute("SELECT username FROM users WHERE id = ?", (user_id,))
        row = await cursor.fetchone()
        username = row[0] if row else "?"
        cursor = await db.execute("DELETE FROM users WHERE id = ?", (user_id,))
        await db.commit()
        if cursor.rowcount == 0:
            raise HTTPException(404, "Kullanici bulunamadi")
    logger.info("User deleted: %s (id: %d)", username, user_id)


# ===== JWT =====

def create_token(username: str, must_change_pw: bool = False) -> str:
    payload = {
        "sub": username,
        "mcp": must_change_pw,
        "exp": datetime.now(timezone.utc) + timedelta(hours=_JWT_EXPIRE_HOURS),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=_JWT_ALGORITHM)


def verify_token(token: str) -> dict | None:
    """Returns {"username": ..., "must_change_pw": ...} if valid, None otherwise."""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[_JWT_ALGORITHM])
        return {
            "username": payload.get("sub"),
            "must_change_pw": payload.get("mcp", False),
        }
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
        return None


# ===== FastAPI Dependencies =====

async def require_auth(request: Request) -> str:
    """Dependency that checks JWT cookie. Returns username."""
    token = request.cookies.get(_COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=401, detail="Giris yapmaniz gerekiyor")
    data = verify_token(token)
    if not data or not data.get("username"):
        raise HTTPException(status_code=401, detail="Oturum suresi doldu")
    return data["username"]


async def ws_auth(ws: WebSocket) -> str | None:
    """Check JWT from cookie for WebSocket connections."""
    token = ws.cookies.get(_COOKIE_NAME)
    if token:
        data = verify_token(token)
        return data.get("username") if data else None
    return None
