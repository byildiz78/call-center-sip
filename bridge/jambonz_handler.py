"""jambonz listen verb WebSocket handler.

Accepts incoming WebSocket connections from jambonz's listen verb,
bridges bidirectional audio to Gemini Live API, records the call,
and creates a ticket when the call ends.
"""

import json
import base64
import asyncio
import logging
import time

from fastapi import WebSocket, WebSocketDisconnect

from bridge.gemini_session import GeminiSession
from bridge.recorder import CallRecorder
from bridge.config import get_settings
from bridge import db
from bridge.ticket import send_webhook

logger = logging.getLogger(__name__)

# Track active calls for monitoring
active_calls: dict[str, dict] = {}


async def handle_jambonz_ws(ws: WebSocket):
    """Handle a single jambonz listen verb WebSocket connection.

    Protocol (subprotocol: audio.jambonz.org):
    1. First message: JSON text frame with session:new event
    2. Subsequent messages: raw L16 PCM binary frames (16kHz 16-bit mono)
    3. To send audio back: raw L16 PCM binary frames (16kHz)
    4. WebSocket closes when call ends
    """
    await ws.accept(subprotocol="audio.jambonz.org")

    # --- Step 1: Receive session:new ---
    try:
        first_msg = await asyncio.wait_for(ws.receive_text(), timeout=10)
    except (asyncio.TimeoutError, WebSocketDisconnect):
        logger.error("No session:new received, closing")
        return

    session_data = json.loads(first_msg)
    call_sid = session_data.get("callSid", f"call-{int(time.time())}")
    caller_number = session_data.get("from", "unknown")

    logger.info("Call started: %s from %s", call_sid, caller_number)

    # --- Step 2: Initialize components ---
    recorder = CallRecorder(call_sid)
    gemini = GeminiSession(call_sid)
    start_time = time.time()

    # Track active call
    active_calls[call_sid] = {
        "caller_number": caller_number,
        "start_time": start_time,
    }

    # Create DB record
    await db.create_call(call_sid, caller_number)

    try:
        await gemini.connect()
    except Exception as e:
        logger.error("Failed to connect Gemini for call %s: %s", call_sid, e)
        await db.fail_call(call_sid, str(e))
        active_calls.pop(call_sid, None)
        return

    # --- Step 3: Bidirectional audio bridge ---
    settings = get_settings()
    max_duration = settings.get("max_call_duration", 90)
    call_should_end = asyncio.Event()

    async def jambonz_to_gemini():
        """Forward raw PCM from jambonz to Gemini as base64."""
        try:
            while not call_should_end.is_set():
                data = await ws.receive()
                if data.get("type") == "websocket.disconnect":
                    break

                raw_bytes = data.get("bytes")
                if raw_bytes:
                    recorder.write_caller(raw_bytes)
                    b64 = base64.b64encode(raw_bytes).decode("ascii")
                    await gemini.send_audio(b64)

                text = data.get("text")
                if text:
                    try:
                        msg = json.loads(text)
                        if msg.get("type") == "dtmf":
                            logger.info("DTMF received: %s", msg.get("dtmf"))
                    except json.JSONDecodeError:
                        pass

        except WebSocketDisconnect:
            logger.info("jambonz disconnected for call %s", call_sid)
        except Exception as e:
            logger.error("jambonz_to_gemini error [%s]: %s", call_sid, e)

    async def gemini_to_jambonz():
        """Forward Gemini audio responses back to jambonz as raw PCM."""
        try:
            from bridge.audio_utils import resample_24k_to_16k

            async for chunk in gemini.receive():
                if call_should_end.is_set():
                    break
                if chunk.audio_b64:
                    pcm_24k = base64.b64decode(chunk.audio_b64)
                    recorder.write_agent(pcm_24k)
                    pcm_16k = resample_24k_to_16k(pcm_24k)
                    await ws.send_bytes(pcm_16k)

                if chunk.text:
                    logger.debug("Gemini said [%s]: %s", call_sid, chunk.text[:100])

                if chunk.end_call:
                    logger.info("Agent said end phrase, closing call %s", call_sid)
                    await asyncio.sleep(1)
                    call_should_end.set()
                    break

        except WebSocketDisconnect:
            pass
        except Exception as e:
            logger.error("gemini_to_jambonz error [%s]: %s", call_sid, e)

    async def timeout_watcher():
        await asyncio.sleep(max_duration)
        if not call_should_end.is_set():
            logger.info("Max duration %ds reached for call %s", max_duration, call_sid)
            call_should_end.set()

    # Run all three concurrently, stop when any finishes
    done, pending = await asyncio.wait(
        [
            asyncio.create_task(jambonz_to_gemini()),
            asyncio.create_task(gemini_to_jambonz()),
            asyncio.create_task(timeout_watcher()),
        ],
        return_when=asyncio.FIRST_COMPLETED,
    )

    # Cancel the remaining task
    for task in pending:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    # --- Step 4: Call ended - cleanup ---
    await gemini.close()
    active_calls.pop(call_sid, None)

    duration = int(time.time() - start_time)
    recording_path = recorder.finalize()
    transcript = gemini.transcript
    summary = gemini.get_issue_summary()
    sentiment = gemini.get_sentiment()
    caller_info = gemini.get_caller_info()

    logger.info(
        "Call ended: %s | duration=%ds | sentiment=%s | recording=%s",
        call_sid, duration, sentiment, recording_path,
    )

    # Update DB
    await db.end_call(call_sid, duration, transcript, summary, recording_path, sentiment)

    # Send webhook
    from bridge.config import PUBLIC_URL, BRIDGE_PORT
    base_url = PUBLIC_URL or f"http://localhost:{BRIDGE_PORT}"
    recording_public_url = f"{base_url}/api/recordings/{call_sid}"
    asyncio.create_task(
        send_webhook(
            call_sid=call_sid,
            caller_number=caller_number,
            caller_name=caller_info["caller_name"],
            business_name=caller_info["business_name"],
            duration=duration,
            summary=summary,
            sentiment=sentiment,
            recording_url=recording_public_url,
            start_time=str(start_time),
        )
    )
