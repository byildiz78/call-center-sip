"""Admin API routes - protected by JWT auth."""

import os
from fastapi import APIRouter, Query, Body, Depends
from fastapi.responses import FileResponse, JSONResponse

from bridge import db
from bridge.auth import require_auth, create_user, update_password, delete_user, list_users
from bridge.config import (
    get_system_prompt, save_system_prompt,
    get_greeting, save_greeting,
    get_settings, save_settings, GEMINI_VOICE,
)
from bridge.jambonz_handler import active_calls

# All routes under this router require authentication
router = APIRouter(prefix="/api", dependencies=[Depends(require_auth)])


# ===== Calls =====

@router.get("/calls")
async def api_list_calls(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    date_from: str = Query("", description="YYYY-MM-DD"),
    date_to: str = Query("", description="YYYY-MM-DD"),
    number: str = Query(""),
    status: str = Query(""),
    sentiment: str = Query(""),
):
    return await db.list_calls(
        page=page, limit=limit,
        date_from=date_from, date_to=date_to,
        number_filter=number, status_filter=status,
        sentiment_filter=sentiment,
    )


@router.get("/stats")
async def api_get_stats():
    stats = await db.get_stats()
    stats["active_calls"] = len(active_calls)
    return stats


@router.get("/active")
async def api_get_active_calls():
    import time
    calls = []
    for sid, info in active_calls.items():
        calls.append({
            "call_sid": sid,
            "caller_number": info.get("caller_number", ""),
            "duration": int(time.time() - info.get("start_time", time.time())),
        })
    return {"active": calls, "count": len(calls)}


# ===== Settings =====

@router.get("/settings/prompt")
async def api_get_settings():
    s = get_settings()
    return {
        "prompt": get_system_prompt(),
        "greeting": get_greeting(),
        "voice": GEMINI_VOICE,
        "max_call_duration": s.get("max_call_duration", 90),
        "end_call_phrase": s.get("end_call_phrase", "iyi günler dilerim"),
        "webhook_url": s.get("webhook_url", ""),
    }


@router.put("/settings/prompt")
async def api_update_settings(body: dict = Body(...)):
    prompt = body.get("prompt", "").strip()
    greeting = body.get("greeting", "").strip()
    if prompt:
        save_system_prompt(prompt)
    if greeting:
        save_greeting(greeting)
    settings_update = {}
    if "max_call_duration" in body:
        settings_update["max_call_duration"] = int(body["max_call_duration"])
    if "end_call_phrase" in body:
        settings_update["end_call_phrase"] = body["end_call_phrase"].strip()
    if "webhook_url" in body:
        url = body["webhook_url"].strip()
        if url:
            # Validate URL format and block internal addresses
            from urllib.parse import urlparse
            parsed = urlparse(url)
            if parsed.scheme not in ("http", "https"):
                return JSONResponse({"error": "Webhook URL http/https olmali"}, status_code=400)
            blocked = ("localhost", "127.0.0.1", "0.0.0.0", "169.254.169.254", "::1")
            if parsed.hostname and parsed.hostname.lower() in blocked:
                return JSONResponse({"error": "Internal adresler kullanilamaz"}, status_code=400)
        settings_update["webhook_url"] = url
    if settings_update:
        save_settings(settings_update)
    return {"status": "ok"}


# ===== Users =====

@router.get("/users")
async def api_list_users():
    return await list_users()


@router.post("/users")
async def api_create_user(body: dict = Body(...)):
    username = body.get("username", "").strip()
    password = body.get("password", "").strip()
    if not username or not password:
        return JSONResponse({"error": "Kullanici adi ve parola gerekli"}, status_code=400)
    return await create_user(username, password)


@router.put("/users/{user_id}")
async def api_update_user(user_id: int, body: dict = Body(...)):
    password = body.get("password", "").strip()
    if not password:
        return JSONResponse({"error": "Yeni parola gerekli"}, status_code=400)
    await update_password(user_id, password)
    return {"status": "ok"}


@router.delete("/users/{user_id}")
async def api_delete_user(user_id: int):
    await delete_user(user_id)
    return {"status": "ok"}


# ===== Call Detail (must be after /users to avoid wildcard conflict) =====

@router.get("/calls/{call_sid}")
async def api_get_call(call_sid: str):
    call = await db.get_call(call_sid)
    if not call:
        return JSONResponse({"error": "Call not found"}, status_code=404)
    return call
