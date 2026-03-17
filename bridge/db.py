"""SQLite database layer for call records. All timestamps are GMT+3."""

import os
import json
import aiosqlite
from datetime import datetime, timezone, timedelta
from bridge.config import DB_PATH

TZ_GMT3 = timezone(timedelta(hours=3))

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    call_sid TEXT UNIQUE NOT NULL,
    caller_number TEXT,
    start_time TEXT,
    end_time TEXT,
    duration_seconds INTEGER DEFAULT 0,
    status TEXT DEFAULT 'in_progress',
    sentiment TEXT DEFAULT 'notr',
    transcript TEXT DEFAULT '[]',
    summary TEXT DEFAULT '',
    recording_path TEXT DEFAULT '',
    ticket_id TEXT DEFAULT '',
    created_at TEXT
);
"""

_ADD_SENTIMENT = "ALTER TABLE calls ADD COLUMN sentiment TEXT DEFAULT 'notr'"
_ADD_AUDIO_DEBUG = "ALTER TABLE calls ADD COLUMN audio_debug TEXT DEFAULT '{}'"


def _now() -> str:
    return datetime.now(TZ_GMT3).strftime("%Y-%m-%d %H:%M:%S")


async def init_db():
    """Create the database and tables if they don't exist."""
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute(_CREATE_TABLE)
        # Migration: add sentiment column if missing
        try:
            await db.execute(_ADD_SENTIMENT)
        except Exception:
            pass
        # Migration: add audio_debug column if missing
        try:
            await db.execute(_ADD_AUDIO_DEBUG)
        except Exception:
            pass
        await db.commit()


async def create_call(call_sid: str, caller_number: str) -> dict:
    now = _now()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO calls (call_sid, caller_number, start_time, created_at) VALUES (?, ?, ?, ?)",
            (call_sid, caller_number, now, now),
        )
        await db.commit()
    return {"call_sid": call_sid, "caller_number": caller_number, "start_time": now}


async def end_call(
    call_sid: str,
    duration: int,
    transcript: list,
    summary: str,
    recording_path: str,
    sentiment: str = "notr",
    audio_debug: dict | None = None,
):
    now = _now()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """UPDATE calls SET
                end_time = ?, duration_seconds = ?, status = 'completed',
                transcript = ?, summary = ?, recording_path = ?, sentiment = ?,
                audio_debug = ?
            WHERE call_sid = ?""",
            (now, duration, json.dumps(transcript, ensure_ascii=False),
             summary, recording_path, sentiment,
             json.dumps(audio_debug or {}, ensure_ascii=False, default=str),
             call_sid),
        )
        await db.commit()


async def update_ticket(call_sid: str, ticket_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE calls SET ticket_id = ? WHERE call_sid = ?",
            (ticket_id, call_sid),
        )
        await db.commit()


async def fail_call(call_sid: str, reason: str = ""):
    now = _now()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE calls SET end_time = ?, status = 'failed', summary = ? WHERE call_sid = ?",
            (now, reason, call_sid),
        )
        await db.commit()


async def get_call(call_sid: str) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM calls WHERE call_sid = ?", (call_sid,))
        row = await cursor.fetchone()
        if row:
            return _row_to_dict(row)
    return None


async def list_calls(
    page: int = 1,
    limit: int = 20,
    date_from: str = "",
    date_to: str = "",
    number_filter: str = "",
    status_filter: str = "",
    sentiment_filter: str = "",
) -> dict:
    offset = (page - 1) * limit
    conditions = []
    params = []

    if date_from:
        conditions.append("DATE(start_time) >= ?")
        params.append(date_from)
    if date_to:
        conditions.append("DATE(start_time) <= ?")
        params.append(date_to)
    if number_filter:
        conditions.append("caller_number LIKE ?")
        params.append(f"%{number_filter}%")
    if status_filter:
        conditions.append("status = ?")
        params.append(status_filter)
    if sentiment_filter:
        conditions.append("sentiment = ?")
        params.append(sentiment_filter)

    where = " AND ".join(conditions)
    where_clause = f"WHERE {where}" if where else ""

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        count_cursor = await db.execute(
            f"SELECT COUNT(*) as cnt FROM calls {where_clause}", params
        )
        count_row = await count_cursor.fetchone()
        total = count_row["cnt"] if count_row else 0

        cursor = await db.execute(
            f"SELECT * FROM calls {where_clause} ORDER BY start_time DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        )
        rows = await cursor.fetchall()

    return {
        "calls": [_row_to_dict(r) for r in rows],
        "total": total,
        "page": page,
        "limit": limit,
        "pages": (total + limit - 1) // limit if total else 0,
    }


async def get_stats() -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        today = datetime.now(TZ_GMT3).strftime("%Y-%m-%d")

        total = await (await db.execute("SELECT COUNT(*) as c FROM calls")).fetchone()
        today_count = await (
            await db.execute(
                "SELECT COUNT(*) as c FROM calls WHERE DATE(start_time) = ?", (today,)
            )
        ).fetchone()
        in_progress = await (
            await db.execute(
                "SELECT COUNT(*) as c FROM calls WHERE status = 'in_progress'"
            )
        ).fetchone()
        avg_dur = await (
            await db.execute(
                "SELECT AVG(duration_seconds) as avg FROM calls WHERE status = 'completed'"
            )
        ).fetchone()

        # Sentiment counts for today
        sent_rows = await (
            await db.execute(
                "SELECT sentiment, COUNT(*) as c FROM calls WHERE DATE(start_time) = ? GROUP BY sentiment",
                (today,),
            )
        ).fetchall()
        sentiments = {"pozitif": 0, "negatif": 0, "notr": 0}
        for r in sent_rows:
            sentiments[r["sentiment"] or "notr"] = r["c"]

        # Total completed today
        completed_today = await (
            await db.execute(
                "SELECT COUNT(*) as c FROM calls WHERE DATE(start_time) = ? AND status = 'completed'",
                (today,),
            )
        ).fetchone()

        # Avg duration today
        avg_dur_today = await (
            await db.execute(
                "SELECT AVG(duration_seconds) as avg FROM calls WHERE DATE(start_time) = ? AND status = 'completed'",
                (today,),
            )
        ).fetchone()

    return {
        "total_calls": total["c"] if total else 0,
        "today_calls": today_count["c"] if today_count else 0,
        "completed_today": completed_today["c"] if completed_today else 0,
        "in_progress": in_progress["c"] if in_progress else 0,
        "avg_duration": round(avg_dur["avg"] or 0) if avg_dur else 0,
        "avg_duration_today": round(avg_dur_today["avg"] or 0) if avg_dur_today else 0,
        "sentiments": sentiments,
    }


async def get_hourly_stats(date_from: str, date_to: str) -> list:
    """Return hourly call counts between two dates (inclusive)."""
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        rows = await (
            await conn.execute(
                """SELECT strftime('%H', start_time) as hour, COUNT(*) as count
                   FROM calls
                   WHERE DATE(start_time) >= ? AND DATE(start_time) <= ?
                   GROUP BY strftime('%H', start_time)
                   ORDER BY hour""",
                (date_from, date_to),
            )
        ).fetchall()
        # Build full 24-hour array
        hourly = {str(i).zfill(2): 0 for i in range(24)}
        for r in rows:
            hourly[r["hour"]] = r["count"]
        return [{"hour": h, "count": c} for h, c in sorted(hourly.items())]


async def get_audio_debug(call_sid: str) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            "SELECT audio_debug FROM calls WHERE call_sid = ?", (call_sid,)
        )
        row = await cursor.fetchone()
        if row and row["audio_debug"]:
            try:
                return json.loads(row["audio_debug"])
            except (json.JSONDecodeError, TypeError):
                return {}
    return None


def _row_to_dict(row, include_debug: bool = False) -> dict:
    d = dict(row)
    if "transcript" in d and isinstance(d["transcript"], str):
        try:
            d["transcript"] = json.loads(d["transcript"])
        except (json.JSONDecodeError, TypeError):
            d["transcript"] = []
    # Strip audio_debug from normal responses (it can be large)
    if not include_debug:
        d.pop("audio_debug", None)
    return d
