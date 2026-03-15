"""Asterisk AudioSocket handler.

Asterisk sends raw audio via TCP (AudioSocket protocol):
- 16-bit signed linear PCM at 8kHz
- Bidirectional: we receive caller audio and send back agent audio

Dialplan: same => n,AudioSocket(127.0.0.1:9093)

Protocol:
- Each frame: 1 byte type + 3 bytes length (big-endian) + payload
- Type 0x01 = UUID (first message, 36 bytes)
- Type 0x10 = Audio (raw PCM slin, 8kHz 16-bit mono)
- Type 0x02 = Silence
- Type 0xFF = Hangup
"""

import asyncio
import base64
import logging
import struct
import time
import uuid

from bridge.gemini_session import GeminiSession
from bridge.recorder import CallRecorder
from bridge.audio_utils import resample
from bridge.config import get_settings, PUBLIC_URL, BRIDGE_PORT
from bridge import db

logger = logging.getLogger(__name__)

AUDIOSOCKET_PORT = 9093

# AudioSocket frame types
TYPE_UUID = 0x01
TYPE_SILENCE = 0x02
TYPE_AUDIO = 0x10
TYPE_ERROR = 0xFF


async def read_frame(reader: asyncio.StreamReader):
    """Read one AudioSocket frame. Returns (type, payload)."""
    header = await reader.readexactly(4)
    frame_type = header[0]
    length = (header[1] << 16) | (header[2] << 8) | header[3]
    payload = await reader.readexactly(length) if length > 0 else b""
    return frame_type, payload


def make_frame(frame_type: int, payload: bytes) -> bytes:
    """Build an AudioSocket frame."""
    length = len(payload)
    header = bytes([frame_type, (length >> 16) & 0xFF, (length >> 8) & 0xFF, length & 0xFF])
    return header + payload


async def handle_audiosocket(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    """Handle one AudioSocket connection (one call)."""
    peer = writer.get_extra_info("peername")
    logger.info("AudioSocket connection from %s", peer)

    # First frame should be UUID
    frame_type, payload = await read_frame(reader)
    if frame_type != TYPE_UUID:
        logger.error("Expected UUID frame, got type %d", frame_type)
        writer.close()
        return

    call_uuid = payload.decode("utf-8", errors="ignore").strip()
    call_sid = f"sip-{call_uuid[:12]}"
    logger.info("AudioSocket call started: %s (uuid: %s)", call_sid, call_uuid)

    start_time = time.time()
    recorder = CallRecorder(call_sid)
    gemini = GeminiSession(call_sid)
    settings = get_settings()
    max_duration = settings.get("max_call_duration", 90)

    # Get caller info from Asterisk (channel variable)
    caller_number = "unknown"

    await db.create_call(call_sid, caller_number)

    try:
        await gemini.connect()
        logger.info("Gemini connected for %s", call_sid)
    except Exception as e:
        logger.error("Gemini connect failed for %s: %s", call_sid, e)
        await db.fail_call(call_sid, str(e))
        writer.close()
        return

    call_ended = asyncio.Event()

    async def asterisk_to_gemini():
        """Read audio from Asterisk, send to Gemini."""
        try:
            while not call_ended.is_set():
                frame_type, payload = await read_frame(reader)
                if frame_type == TYPE_AUDIO and payload:
                    # payload is slin 8kHz 16-bit PCM
                    recorder.write_caller(payload)
                    # Resample 8kHz → 16kHz for Gemini
                    pcm_16k = resample(payload, 8000, 16000)
                    b64 = base64.b64encode(pcm_16k).decode("ascii")
                    await gemini.send_audio(b64)
                elif frame_type == TYPE_ERROR or frame_type == TYPE_SILENCE:
                    pass  # ignore
                elif frame_type == TYPE_UUID:
                    pass  # duplicate UUID, ignore
        except (asyncio.IncompleteReadError, ConnectionResetError):
            logger.info("Asterisk disconnected for %s", call_sid)
        except Exception as e:
            logger.error("asterisk_to_gemini error [%s]: %s", call_sid, e)
        finally:
            call_ended.set()

    async def gemini_to_asterisk():
        """Read audio from Gemini, send to Asterisk."""
        try:
            async for chunk in gemini.receive():
                if call_ended.is_set():
                    break
                if chunk.audio_b64:
                    pcm_24k = base64.b64decode(chunk.audio_b64)
                    recorder.write_agent(pcm_24k)
                    # Resample 24kHz → 8kHz for Asterisk
                    pcm_8k = resample(pcm_24k, 24000, 8000)
                    # Send as AudioSocket audio frame
                    frame = make_frame(TYPE_AUDIO, pcm_8k)
                    writer.write(frame)
                    await writer.drain()
                if chunk.end_call:
                    logger.info("Agent end phrase for %s", call_sid)
                    await asyncio.sleep(1)
                    call_ended.set()
                    break
        except (ConnectionResetError, BrokenPipeError):
            pass
        except Exception as e:
            logger.error("gemini_to_asterisk error [%s]: %s", call_sid, e)
        finally:
            call_ended.set()

    async def timeout_watcher():
        await asyncio.sleep(max_duration)
        if not call_ended.is_set():
            logger.info("Max duration %ds for %s", max_duration, call_sid)
            call_ended.set()

    done, pending = await asyncio.wait(
        [
            asyncio.create_task(asterisk_to_gemini()),
            asyncio.create_task(gemini_to_asterisk()),
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
    writer.close()
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


async def start_audiosocket_server():
    """Start the AudioSocket TCP server."""
    server = await asyncio.start_server(
        handle_audiosocket, "127.0.0.1", AUDIOSOCKET_PORT,
    )
    logger.info("AudioSocket server listening on 127.0.0.1:%d", AUDIOSOCKET_PORT)
    async with server:
        await server.serve_forever()
