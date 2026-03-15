"""Freeswitch audio fork WebSocket handler.

Freeswitch forkAudioStart sends:
1. First message: JSON text with metadata (if provided)
2. Subsequent: binary L16 PCM audio frames (8kHz or 16kHz)

This handler bridges the audio to Gemini Live API,
records the call, and creates a ticket when done.
"""

import json
import base64
import asyncio
import logging
import time

from fastapi import WebSocket, WebSocketDisconnect

from bridge.gemini_session import GeminiSession
from bridge.recorder import CallRecorder
from bridge.audio_utils import resample
from bridge.config import get_settings
from bridge import db

logger = logging.getLogger(__name__)


async def handle_freeswitch_ws(ws: WebSocket):
    """Handle a freeswitch audio fork WebSocket connection."""
    await ws.accept()

    call_sid = f"sip-{int(time.time())}"
    caller_number = "unknown"
    start_time = time.time()

    # First message might be metadata JSON or binary audio
    try:
        first_msg = await asyncio.wait_for(ws.receive(), timeout=10)
    except (asyncio.TimeoutError, WebSocketDisconnect):
        logger.error("No data received from freeswitch, closing")
        return

    # Check if first message is metadata
    first_audio = None
    if first_msg.get("text"):
        try:
            meta = json.loads(first_msg["text"])
            call_sid = meta.get("callSid", call_sid)
            caller_number = meta.get("from", caller_number)
            logger.info("Received metadata: callSid=%s from=%s", call_sid, caller_number)
        except json.JSONDecodeError:
            pass
    elif first_msg.get("bytes"):
        first_audio = first_msg["bytes"]

    # Initialize components
    recorder = CallRecorder(call_sid)
    gemini = GeminiSession(call_sid)
    settings = get_settings()
    max_duration = settings.get("max_call_duration", 90)

    await db.create_call(call_sid, caller_number)

    try:
        await gemini.connect()
        logger.info("Gemini connected for SIP call %s", call_sid)
    except Exception as e:
        logger.error("Failed to connect Gemini for call %s: %s", call_sid, e)
        await db.fail_call(call_sid, str(e))
        return

    # If first message was audio, process it
    if first_audio:
        recorder.write_caller(first_audio)
        # Resample 8kHz to 16kHz for Gemini
        pcm_16k = resample(first_audio, 8000, 16000)
        b64 = base64.b64encode(pcm_16k).decode("ascii")
        await gemini.send_audio(b64)

    call_should_end = asyncio.Event()

    async def fs_to_gemini():
        """Forward freeswitch audio to Gemini."""
        try:
            while not call_should_end.is_set():
                msg = await ws.receive()
                if msg.get("type") == "websocket.disconnect":
                    break
                raw = msg.get("bytes")
                if raw:
                    recorder.write_caller(raw)
                    pcm_16k = resample(raw, 8000, 16000)
                    b64 = base64.b64encode(pcm_16k).decode("ascii")
                    await gemini.send_audio(b64)
        except WebSocketDisconnect:
            logger.info("Freeswitch disconnected for call %s", call_sid)
        except Exception as e:
            logger.error("fs_to_gemini error [%s]: %s", call_sid, e)

    async def gemini_to_fs():
        """Forward Gemini audio to freeswitch."""
        try:
            from bridge.audio_utils import resample_24k_to_16k
            async for chunk in gemini.receive():
                if call_should_end.is_set():
                    break
                if chunk.audio_b64:
                    pcm_24k = base64.b64decode(chunk.audio_b64)
                    recorder.write_agent(pcm_24k)
                    # Resample 24kHz -> 8kHz for freeswitch
                    pcm_8k = resample(pcm_24k, 24000, 8000)
                    try:
                        await ws.send_bytes(pcm_8k)
                    except Exception:
                        break
                if chunk.end_call:
                    logger.info("Agent end phrase, closing call %s", call_sid)
                    await asyncio.sleep(1)
                    call_should_end.set()
                    break
        except WebSocketDisconnect:
            pass
        except Exception as e:
            logger.error("gemini_to_fs error [%s]: %s", call_sid, e)

    async def timeout_watcher():
        await asyncio.sleep(max_duration)
        if not call_should_end.is_set():
            logger.info("Max duration %ds for call %s", max_duration, call_sid)
            call_should_end.set()

    done, pending = await asyncio.wait(
        [
            asyncio.create_task(fs_to_gemini()),
            asyncio.create_task(gemini_to_fs()),
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

    # Cleanup
    await gemini.close()

    duration = int(time.time() - start_time)
    recording_path = recorder.finalize()
    transcript = gemini.transcript
    summary = gemini.get_issue_summary()
    sentiment = gemini.get_sentiment()
    caller_info = gemini.get_caller_info()

    await db.end_call(call_sid, duration, transcript, summary, recording_path, sentiment)
    logger.info("SIP call ended: %s | %ds | %s", call_sid, duration, sentiment)

    from bridge.ticket import send_webhook
    from bridge.config import PUBLIC_URL, BRIDGE_PORT
    base_url = PUBLIC_URL or f"http://localhost:{BRIDGE_PORT}"
    recording_public_url = f"{base_url}/api/recordings/{call_sid}"
    asyncio.create_task(send_webhook(
        call_sid=call_sid,
        caller_number=caller_number,
        caller_name=caller_info["caller_name"],
        business_name=caller_info["business_name"],
        duration=duration,
        summary=summary,
        sentiment=sentiment,
        recording_url=recording_public_url,
        start_time=str(start_time),
    ))
