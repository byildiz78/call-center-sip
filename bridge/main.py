"""AI Call Center Bridge Server - Main FastAPI Application.

Provides:
- /jambonz-ws          : WebSocket for jambonz (PUBLIC - SIP)
- /api/recordings/*    : Recording playback (PUBLIC - link access)
- /login               : Login page (PUBLIC)
- /api/auth/*          : Auth endpoints (PUBLIC)
- /admin/static/*      : Static files (PUBLIC)
- /ws                  : Browser test WebSocket (AUTH required)
- /admin               : Dashboard (AUTH required)
- /api/*               : Admin API (AUTH required via router dependency)
"""

import os
import re
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, Request, Depends
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from bridge import db
from bridge.auth import (
    init_users, verify_user, create_token, verify_token,
    require_auth, ws_auth, _COOKIE_NAME,
    _check_rate_limit, _record_failed_attempt, _clear_attempts,
)
from bridge.config import BRIDGE_HOST, BRIDGE_PORT, RECORDINGS_DIR
from bridge.jambonz_handler import handle_jambonz_ws
from bridge.admin.routes import router as admin_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

STATIC_DIR = os.path.join(os.path.dirname(__file__), "admin", "static")


@asynccontextmanager
async def lifespan(app: FastAPI):
    os.makedirs(RECORDINGS_DIR, exist_ok=True)
    os.makedirs(os.path.dirname(db.DB_PATH) or ".", exist_ok=True)
    await db.init_db()
    await init_users()
    logger.info("Bridge server started - recordings: %s", RECORDINGS_DIR)
    yield
    logger.info("Bridge server shutting down")


app = FastAPI(title="Robotpos AI Call Center Bridge", lifespan=lifespan)


# ===== Security Headers Middleware =====
from starlette.middleware.base import BaseHTTPMiddleware

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        return response

app.add_middleware(SecurityHeadersMiddleware)


# ===== PUBLIC: Auth endpoints =====

@app.get("/login", response_class=HTMLResponse)
async def login_page():
    with open(os.path.join(STATIC_DIR, "login.html"), "r", encoding="utf-8") as f:
        return f.read()


@app.post("/api/auth/login")
async def login(request: Request):
    client_ip = request.client.host if request.client else "unknown"
    _check_rate_limit(client_ip)

    body = await request.json()
    username = body.get("username", "").strip()[:50]
    password = body.get("password", "")[:255]
    if not username or not password:
        return JSONResponse({"error": "Kullanici adi ve parola gerekli"}, status_code=400)

    user = await verify_user(username, password)
    if not user:
        _record_failed_attempt(client_ip)
        return JSONResponse({"error": "Gecersiz kullanici adi veya parola"}, status_code=401)

    _clear_attempts(client_ip)
    token = create_token(username, must_change_pw=user.get("must_change_pw", False))
    response = JSONResponse({
        "status": "ok",
        "username": username,
        "must_change_pw": user.get("must_change_pw", False),
    })
    response.set_cookie(
        _COOKIE_NAME, token,
        httponly=True, samesite="lax", max_age=86400,
    )
    return response


@app.post("/api/auth/logout")
async def logout():
    response = JSONResponse({"status": "ok"})
    response.delete_cookie(_COOKIE_NAME)
    return response


# ===== PUBLIC: Recordings (link access) =====

@app.get("/api/recordings/{call_sid}")
async def public_recording(call_sid: str):
    # Validate call_sid format (prevent injection)
    if not re.match(r"^[a-zA-Z0-9_-]+$", call_sid):
        return JSONResponse({"error": "Invalid call_sid"}, status_code=400)
    call = await db.get_call(call_sid)
    if not call or not call.get("recording_path"):
        return JSONResponse({"error": "Recording not found"}, status_code=404)
    path = call["recording_path"]
    # Path traversal protection
    real_path = os.path.realpath(path)
    allowed_dir = os.path.realpath(RECORDINGS_DIR)
    if not real_path.startswith(allowed_dir):
        logger.warning("Path traversal attempt: %s", path)
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    if not os.path.exists(real_path):
        return JSONResponse({"error": "Recording file missing"}, status_code=404)
    return FileResponse(
        real_path,
        media_type="audio/wav",
        headers={"Content-Disposition": "inline"},
    )


# ===== PUBLIC: jambonz WebSocket =====

@app.websocket("/jambonz-ws")
async def jambonz_websocket(ws: WebSocket):
    await handle_jambonz_ws(ws)


# ===== PROTECTED: Admin API =====

app.include_router(admin_router)


# ===== PROTECTED: Admin UI =====

@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    token = request.cookies.get(_COOKIE_NAME)
    if not token or not verify_token(token):
        return RedirectResponse("/login", status_code=302)
    with open(os.path.join(STATIC_DIR, "index.html"), "r", encoding="utf-8") as f:
        return f.read()


# ===== PROTECTED: Browser test WebSocket =====

@app.websocket("/ws")
async def browser_websocket(ws: WebSocket):
    """Browser testing endpoint - requires auth cookie."""
    import json
    import base64
    import asyncio
    import time as _time
    from bridge.gemini_session import GeminiSession
    from bridge.recorder import CallRecorder
    from bridge.config import get_settings

    # Check auth
    username = await ws_auth(ws)
    if not username:
        logger.warning("WS auth failed - cookies: %s", dict(ws.cookies))
        await ws.accept()
        await ws.send_json({"type": "error", "message": "Oturum gecersiz, tekrar giris yapin"})
        await ws.close(code=4001, reason="Unauthorized")
        return
    logger.info("WS auth OK for user: %s", username)

    await ws.accept()

    call_sid = f"test-{int(_time.time())}"
    gemini = GeminiSession(call_sid)
    recorder = CallRecorder(call_sid)
    start_time = _time.time()
    settings = get_settings()
    max_duration = settings.get("max_call_duration", 90)

    await db.create_call(call_sid, "browser-test")

    try:
        await gemini.connect()
        await ws.send_json({"type": "setup_complete"})

        call_should_end = asyncio.Event()

        async def client_to_gemini():
            try:
                while not call_should_end.is_set():
                    data = await ws.receive_text()
                    msg = json.loads(data)
                    if msg.get("type") == "audio":
                        pcm = base64.b64decode(msg["data"])
                        recorder.write_caller(pcm)
                        await gemini.send_audio(msg["data"])
                    elif msg.get("type") == "end":
                        break
            except Exception:
                pass

        async def gemini_to_client():
            try:
                async for chunk in gemini.receive():
                    if call_should_end.is_set():
                        break
                    if chunk.audio_b64:
                        pcm_24k = base64.b64decode(chunk.audio_b64)
                        recorder.write_agent(pcm_24k)
                        await ws.send_json({
                            "type": "audio",
                            "data": chunk.audio_b64,
                            "mimeType": "audio/pcm;rate=24000",
                        })
                    if chunk.text:
                        await ws.send_json({"type": "text", "text": chunk.text, "role": chunk.role})
                    if chunk.turn_complete:
                        await ws.send_json({"type": "turn_complete"})
                    if chunk.end_call:
                        logger.info("Agent said end phrase, closing call %s", call_sid)
                        await asyncio.sleep(1)
                        await ws.send_json({"type": "call_ended", "reason": "agent_end_phrase"})
                        call_should_end.set()
                        break
            except Exception:
                pass

        async def timeout_watcher():
            await asyncio.sleep(max_duration)
            if not call_should_end.is_set():
                logger.info("Max duration %ds reached for call %s", max_duration, call_sid)
                try:
                    await ws.send_json({"type": "call_ended", "reason": "max_duration"})
                except Exception:
                    pass
                call_should_end.set()

        done, pending = await asyncio.wait(
            [
                asyncio.create_task(client_to_gemini()),
                asyncio.create_task(gemini_to_client()),
                asyncio.create_task(timeout_watcher()),
            ],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
    except Exception as e:
        logger.error("Browser WS error: %s", e)
        await db.fail_call(call_sid, str(e))
        try:
            await ws.send_json({"type": "error", "message": str(e)})
        except Exception:
            pass
    finally:
        await gemini.close()

        duration = int(_time.time() - start_time)
        recording_path = recorder.finalize()
        transcript = gemini.transcript
        summary = gemini.get_issue_summary()
        sentiment = gemini.get_sentiment()
        caller_info = gemini.get_caller_info()

        await db.end_call(call_sid, duration, transcript, summary, recording_path, sentiment)
        logger.info("Test call ended: %s | %ds | %s | %s", call_sid, duration, sentiment, recording_path)

        from bridge.ticket import send_webhook
        from bridge.config import PUBLIC_URL
        base_url = PUBLIC_URL or f"http://localhost:{BRIDGE_PORT}"
        recording_public_url = f"{base_url}/api/recordings/{call_sid}"
        asyncio.create_task(send_webhook(
            call_sid=call_sid,
            caller_number="browser-test",
            caller_name=caller_info["caller_name"],
            business_name=caller_info["business_name"],
            duration=duration,
            summary=summary,
            sentiment=sentiment,
            recording_url=recording_public_url,
            start_time=str(start_time),
        ))


# ===== Static files =====

if os.path.isdir(STATIC_DIR):
    app.mount("/admin/static", StaticFiles(directory=STATIC_DIR), name="admin-static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=BRIDGE_HOST, port=BRIDGE_PORT)
